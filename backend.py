import re
import os
import json
import shutil
import socket
import concurrent.futures
import requests
import io
import csv
import datetime
from datetime import datetime as dt, timedelta, date
from collections import defaultdict
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Tuple

try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
    DRIVE_AVAILABLE = True
except ImportError:
    DRIVE_AVAILABLE = False
    print("Warning: Google Drive libraries not found.")

TIME_FORMAT = "%d/%m/%Y"
SETTINGS_FILE = "settings.json"
BACKUP_DIR = "backups"
SCOPES = ['https://www.googleapis.com/auth/drive.file']

@dataclass
class Transaction:
    amount: float
    tags: List[str]
    def to_string(self) -> str:
        base = f"{self.amount:g}"
        if self.tags: return f"{base}({', '.join(self.tags)})"
        return base

@dataclass
class DailyRecord:
    date: date
    balance_snapshot: Optional[float] = None
    transactions: List[Transaction] = field(default_factory=list)

    @property
    def total_change(self) -> float: return sum(t.amount for t in self.transactions)
    @property
    def income(self) -> float: return sum(t.amount for t in self.transactions if t.amount > 0)
    @property
    def expense(self) -> float: return sum(t.amount for t in self.transactions if t.amount < 0)

    def to_dict(self):
        return {
            "date": self.date.strftime(TIME_FORMAT),
            "day_name": self.date.strftime("%a"),
            "net_change": self.total_change,
            "income": self.income,
            "expense": self.expense,
            "balance_snapshot": self.balance_snapshot,
            "transactions": [{"amount": t.amount, "tags": t.tags} for t in self.transactions]
        }
    
    def to_file_line(self) -> str:
        day_str = self.date.strftime("%a")
        date_str = self.date.strftime(TIME_FORMAT)
        bal_str = f"{self.balance_snapshot:g}" if self.balance_snapshot is not None else ""
        trans_parts = [t.to_string() for t in self.transactions]
        trans_str = " ".join(trans_parts)
        return f"{day_str} {date_str}: {bal_str.center(5)} :{trans_str}"

class QueryParser:
    def compile_query(self, query: str):
        if not query or not query.strip(): return None
        q = query
        def repl_date(m):
            d, m_month, y = m.group(1).split('/')
            return f"datetime.date({int(y)}, {int(m_month)}, {int(d)})"
        try:
            q = re.sub(r"(\d{1,2}/\d{1,2}/\d{4})", repl_date, q)
            q = q.replace("&&", " and ").replace("||", " or ")
            q = re.sub(r"tag\s*==\s*['\"]([^'\"]+)['\"]", r"'\1' in tags", q)
            q = re.sub(r"tag\s*!=\s*['\"]([^'\"]+)['\"]", r"'\1' not in tags", q)
            return compile(q, '<string>', 'eval')
        except Exception: return None 

    def evaluate(self, transaction: Transaction, rec_date: date, compiled_q) -> bool:
        if compiled_q is None: return True
        env = {
            "date": rec_date,
            "amount": transaction.amount,
            "tags": transaction.tags,
            "day": rec_date.strftime("%a").lower(),
            "datetime": datetime,
            "income": transaction.amount > 0,
            "expense": transaction.amount < 0,
        }
        try: return bool(eval(compiled_q, {"__builtins__": {}}, env))
        except Exception: return False 

class DriveManager:
    def __init__(self, creds_path, token_path):
        self.creds_path = creds_path
        self.token_path = token_path
        self.service = None
        self.folder_name = "mLoger_Backups"
    
    def authenticate(self):
        if not DRIVE_AVAILABLE: raise Exception("Google Drive libs missing")
        creds = None
        if os.path.exists(self.token_path):
            creds = Credentials.from_authorized_user_file(self.token_path, SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token: creds.refresh(Request())
            else:
                if not os.path.exists(self.creds_path): raise FileNotFoundError(f"Credentials not found")
                flow = InstalledAppFlow.from_client_secrets_file(self.creds_path, SCOPES)
                creds = flow.run_local_server(port=0)
            with open(self.token_path, 'w') as token: token.write(creds.to_json())
        self.service = build('drive', 'v3', credentials=creds)
        return True

    def _get_folder_id(self):
        results = self.service.files().list(q=f"mimeType='application/vnd.google-apps.folder' and name='{self.folder_name}' and trashed=false", fields="files(id, name)").execute()
        items = results.get('files', [])
        if not items:
            file_metadata = {'name': self.folder_name, 'mimeType': 'application/vnd.google-apps.folder'}
            file = self.service.files().create(body=file_metadata, fields='id').execute()
            return file.get('id')
        else: return items[0]['id']

    def upload_file(self, filepath):
        if not self.service: self.authenticate()
        folder_id = self._get_folder_id()
        name = f"cache_{dt.now().strftime('%Y-%m-%d_%H-%M-%S')}.txt"
        file_metadata = {'name': name, 'parents': [folder_id]}
        media = MediaFileUpload(filepath, mimetype='text/plain')
        file = self.service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        return file.get('id'), name

    def list_versions(self):
        if not self.service: self.authenticate()
        folder_id = self._get_folder_id()
        results = self.service.files().list(q=f"'{folder_id}' in parents and trashed=false", orderBy="createdTime desc", fields="files(id, name, createdTime, size)").execute()
        return results.get('files', [])

    def download_content(self, file_id):
        if not self.service: self.authenticate()
        request = self.service.files().get_media(fileId=file_id)
        file_io = io.BytesIO()
        downloader = MediaIoBaseDownload(file_io, request)
        done = False
        while done is False: status, done = downloader.next_chunk()
        return file_io.getvalue().decode('utf-8')

class ExpenseManager:
    def __init__(self, filepath="cache.txt"):
        self.filepath = filepath
        self.records: Dict[date, DailyRecord] = {}
        self.query_engine = QueryParser()
        self.settings = self._load_settings()
        
        if not os.path.exists(BACKUP_DIR): os.makedirs(BACKUP_DIR)
        if os.path.exists(filepath): self.reload_from_file()
        
        self.drive_mgr = None
        if self.settings.get('drive_enabled'): self._init_drive()

    def _init_drive(self):
        creds = self.settings.get('drive_creds_path', 'credentials.json')
        token = self.settings.get('drive_token_path', 'token.json')
        self.drive_mgr = DriveManager(creds, token)

    def get_drive_settings(self): return {"enabled":self.settings['drive_enabled']}
    def save_drive_settings(self, enabled, creds_path, token_path):
        self.settings['drive_enabled'] = enabled
        self.settings['drive_creds_path'] = creds_path
        self.settings['drive_token_path'] = token_path
        self._save_settings()
        if enabled:
            try:
                self._init_drive()
                self.drive_mgr.authenticate()
                return {"status": "success", "message": "Drive authenticated"}
            except Exception as e:
                self.settings['drive_enabled'] = False
                self._save_settings()
                return {"status": "error", "message": str(e)}
        return {"status": "success", "message": "Settings saved"}

    def drive_push(self):
        if not self.drive_mgr: return {"status": "error", "message": "Drive not enabled"}
        try:
            self.save_to_file()
            fid, name = self.drive_mgr.upload_file(self.filepath)
            self.settings['last_synced'] = dt.now().strftime("%Y-%m-%d %H:%M:%S")
            self._save_settings()
            return {"status": "success", "message": f"Uploaded {name}", "last_synced": self.settings['last_synced']}
        except Exception as e: return {"status": "error", "message": str(e)}

    def drive_list_versions(self):
        if not self.drive_mgr: return {"status": "error", "message": "Drive not enabled"}
        try: return {"status": "success", "files": self.drive_mgr.list_versions()}
        except Exception as e: return {"status": "error", "message": str(e)}

    def drive_preview_version(self, file_id):
        if not self.drive_mgr: return {"status": "error", "message": "Drive not enabled"}
        try:
            content = self.drive_mgr.download_content(file_id)
            lines = content.splitlines()
            count = sum(1 for l in lines if self._parse_line(l))
            preview_text = f"Records: {count}\nFirst 5 lines:\n" + "\n".join(lines[:5])
            return {"status": "success", "content": content, "preview": preview_text}
        except Exception as e: return {"status": "error", "message": str(e)}

    def drive_apply_version(self, file_id):
        if not self.drive_mgr: return {"status": "error", "message": "Drive not enabled"}
        try:
            self.create_backup("predrive_restore")
            content = self.drive_mgr.download_content(file_id)
            with open(self.filepath, 'w', encoding='utf-8') as f: f.write(content)
            self.reload_from_file()
            self.settings['last_synced'] = dt.now().strftime("%Y-%m-%d %H:%M:%S")
            self._save_settings()
            return {"status": "success", "message": "Version applied"}
        except Exception as e: return {"status": "error", "message": str(e)}

    def scan_network_and_sync(self):
        try:
            discovered_url = self.get_source_url()
            messages = []
            if discovered_url:
                messages.append(f"Found Server: {discovered_url}")
                if discovered_url not in self.settings["sources"]:
                    self.add_source(discovered_url)
                    messages.append("Added to sources list.")
            else: messages.append("No local server found.")
            messages.extend(self.sync_all_sources())
            return {"status": "success", "message": "\n".join(messages)}
        except Exception as e: return {"status": "error", "message": str(e)}

    def get_local_ip(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try: s.connect(('10.255.255.255', 1)); IP = s.getsockname()[0]
        except Exception: IP = '127.0.0.1'
        finally: s.close()
        return IP

    def _check_server(self, ip):
        url = f"http://{ip}:5000/data/cache.txt"
        try: requests.head(url, timeout=0.2); return url
        except: return None

    def get_source_url(self):
        local_ip = self.get_local_ip()
        if local_ip == '127.0.0.1': return None
        base_ip = ".".join(local_ip.split(".")[:-1])
        ips = [f"{base_ip}.{i}" for i in range(1, 255)]
        found_url = None
        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
            for url in executor.map(self._check_server, ips):
                if url: found_url = url; break 
        return found_url

    def _load_settings(self):
        if os.path.exists(SETTINGS_FILE):
            try: 
                with open(SETTINGS_FILE, 'r') as f: return json.load(f)
            except: pass
        return {
            "sources": [], 
            "auto_fetch": True, 
            "drive_enabled": False,
            "chart_lines": {"balance": [], "main": [], "analytics": []}
        }

    def _save_settings(self):
        with open(SETTINGS_FILE, 'w') as f: json.dump(self.settings, f, indent=4)

    def add_source(self, url: str):
        if url not in self.settings["sources"]: self.settings["sources"].append(url); self._save_settings()
        return self.settings["sources"]
    def remove_source(self, url: str):
        if url in self.settings["sources"]: self.settings["sources"].remove(url); self._save_settings()
        return self.settings["sources"]
    def get_sources(self): return self.settings["sources"]

    def get_chart_lines(self): return self.settings.get("chart_lines", {"balance": [], "main": [], "analytics": []})
    def save_chart_lines(self, lines_data):
        self.settings["chart_lines"] = lines_data
        self._save_settings()
        return {"status": "success"}

    def sync_all_sources(self):
        results = []
        for src in self.settings["sources"]:
            res = self.fetch_and_merge(src)
            status = "Updated" if res.get("updated", 0) > 0 else "No new data"
            if res.get("status") == "error": status = f"Error: {res.get('message')}"
            results.append(f"{src}: {status}")
        return results

    def create_backup(self, reason="manual"):
        if not os.path.exists(self.filepath): return
        timestamp = dt.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"cache_{timestamp}_{reason}.txt"
        shutil.copy(self.filepath, os.path.join(BACKUP_DIR, backup_name))
        return backup_name

    def get_backups(self):
        if not os.path.exists(BACKUP_DIR): return []
        return sorted(os.listdir(BACKUP_DIR), reverse=True)[:10]

    def restore_backup(self, backup_filename):
        src = os.path.join(BACKUP_DIR, backup_filename)
        if os.path.exists(src):
            self.create_backup("prerestore")
            shutil.copy(src, self.filepath)
            self.reload_from_file()
            return {"status": "success", "message": f"Restored {backup_filename}"}
        return {"status": "error", "message": "Backup file not found"}

    def _parse_line(self, line: str) -> Optional[DailyRecord]:
        line = line.strip()
        if not line or line.startswith("//") or line.startswith("#"): return None
        if "#" in line: line = line.split("#")[0]
        try:
            parts = line.split(":")
            if len(parts) < 3: return None
            date_part, balance_part, trans_part = parts[0], parts[1], ":".join(parts[2:])
            d_match = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", date_part)
            if not d_match: return None
            rec_date = dt.strptime(d_match.group(1), TIME_FORMAT).date()
            balance = float(balance_part.strip()) if balance_part.strip() else None
            
            transactions = []
            for match in re.finditer(r"(?P<amt>[+\-]?\s*\d+(?:\.\d+)?)\s*(?:\((?P<tags>[^)]*)\))?", trans_part):
                amount_val = float(match.group("amt").replace(" ", ""))
                tag_list = [t.strip() for t in match.group("tags").split(",") if t.strip()] if match.group("tags") else []
                transactions.append(Transaction(amount_val, tag_list))
            return DailyRecord(rec_date, balance, transactions)
        except Exception: return None

    def reload_from_file(self):
        self.records = {}
        if not os.path.exists(self.filepath): return
        with open(self.filepath, "r", encoding="utf-8") as f:
            for line in f:
                rec = self._parse_line(line)
                if rec: self.records[rec.date] = rec
        return {"status": "success"}

    def save_to_file(self, backup=True):
        if backup: self.create_backup("autosave")
        with open(self.filepath, "w", encoding="utf-8") as f:
            for rec in sorted(self.records.values(), key=lambda r: r.date):
                f.write(rec.to_file_line() + "\n")
        return {"status": "success"}

    def update_transaction(self, date_str: str, trans_index: int, amount: float, tags: List[str]):
        try:
            target_date = dt.strptime(date_str, TIME_FORMAT).date()
            if target_date not in self.records: return {"status": "error", "message": "Date not found"}
            record = self.records[target_date]
            if trans_index < 0 or trans_index >= len(record.transactions): return {"status": "error", "message": "Idx out of bounds"}
            record.transactions[trans_index] = Transaction(amount, tags)
            self.save_to_file()
            return {"status": "success"}
        except Exception as e: return {"status": "error", "message": str(e)}

    def add_transaction(self, date_str: str, amount: float, tags: List[str]):
        try:
            target_date = dt.strptime(date_str, TIME_FORMAT).date()
            if target_date not in self.records: self.records[target_date] = DailyRecord(target_date, None, [])
            self.records[target_date].transactions.append(Transaction(amount, tags))
            self.save_to_file()
            return {"status": "success"}
        except Exception as e: return {"status": "error", "message": str(e)}
            
    def delete_transaction(self, date_str: str, trans_index: int):
        try:
            target_date = dt.strptime(date_str, TIME_FORMAT).date()
            if target_date in self.records and 0 <= trans_index < len(self.records[target_date].transactions):
                self.records[target_date].transactions.pop(trans_index)
                self.save_to_file()
                return {"status": "success"}
            return {"status": "error", "message": "Record not found"}
        except Exception as e: return {"status": "error", "message": str(e)}

    def fetch_and_merge(self, source: str):
        lines = []
        if source.startswith("http"):
            try: lines = requests.get(source, timeout=2).text.splitlines()
            except Exception as e: return {"status": "error", "message": str(e)}
        elif os.path.exists(source):
            with open(source, "r", encoding="utf-8") as f: lines = f.readlines()
        else: return {"status": "error", "message": "Source not found"}
        
        updated = 0
        for line in lines:
            rec = self._parse_line(line)
            if rec: self.records[rec.date] = rec; updated += 1
        self.save_to_file()
        return {"status": "success", "updated": updated, "message": f"Merged {updated} records"}

    def get_date_bounds(self) -> Dict[str, Any]:
        if not self.records:
            today = date.today().strftime(TIME_FORMAT)
            return {"start": today, "end": today, "total_days": 0}
        sorted_dates = sorted(self.records.keys())
        start_date = sorted_dates[0]
        end_date = max(sorted_dates[-1], date.today()) 
        return {
            "start": start_date.strftime(TIME_FORMAT),
            "end": end_date.strftime(TIME_FORMAT),
            "total_days": (end_date - start_date).days
        }

    def _get_filtered_records(self, filter_query: str) -> List[Tuple[DailyRecord, float]]:
        sorted_recs = sorted(self.records.values(), key=lambda r: r.date)
        running_bal = 0
        day_balances = {}
        for rec in sorted_recs:
            running_bal += rec.total_change
            day_balances[rec.date] = running_bal

        result = []
        compiled_q = self.query_engine.compile_query(filter_query)
        
        for rec in sorted_recs:
            filtered_trans = []
            if compiled_q is None: filtered_trans = rec.transactions
            else:
                for t in rec.transactions:
                    if self.query_engine.evaluate(t, rec.date, compiled_q): filtered_trans.append(t)
            
            if filtered_trans or (not filter_query.strip() and not rec.transactions):
                temp_rec = DailyRecord(rec.date, rec.balance_snapshot, filtered_trans)
                result.append((temp_rec, day_balances[rec.date]))
        return result

    def get_dashboard_data(self, filter_query: str = "") -> Dict[str, Any]:
        filtered = self._get_filtered_records(filter_query)
        records_out = []
        for rec, hist_bal in reversed(filtered):
            d = rec.to_dict()
            d['historical_balance'] = hist_bal
            records_out.append(d)

        total_income = 0
        total_expense = 0
        tag_spending = defaultdict(float)
        trans_count = 0
        
        for rec, _ in filtered:
            for t in rec.transactions:
                trans_count += 1
                if t.amount > 0: total_income += t.amount
                elif t.amount < 0:
                    total_expense += t.amount
                    tag_key = ", ".join(sorted(t.tags)) if t.tags else "Untagged"
                    tag_spending[tag_key] += abs(t.amount)
                    
        stats_out = {
            "record_count": trans_count,
            "total_income": round(total_income, 2),
            "total_expense": round(total_expense, 2),
            "net_savings": round(total_income + total_expense, 2),
            "tag_breakdown": dict(sorted(tag_spending.items(), key=lambda x: x[1], reverse=True)[:15])
        }
        current_balance = round(sum(r.total_change for r in self.records.values()), 2)
        
        return {
            "records": records_out,
            "stats": stats_out,
            "global_balance": current_balance
        }

    def export_csv(self, filter_query: str = "") -> dict:
        try:
            filtered = self._get_filtered_records(filter_query)
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["Date", "Day", "Historical Balance", "Daily Net Change", "Total Income", "Total Expense", "Transactions (Amount [Tags])"])
            
            for rec, hist_bal in filtered:
                trans_strs = []
                for t in rec.transactions:
                    tag_str = f" [{', '.join(t.tags)}]" if t.tags else ""
                    trans_strs.append(f"{t.amount}{tag_str}")
                    
                writer.writerow([
                    rec.date.strftime(TIME_FORMAT),
                    rec.date.strftime("%a"),
                    round(hist_bal, 2),
                    round(rec.total_change, 2),
                    round(rec.income, 2),
                    round(rec.expense, 2),
                    " | ".join(trans_strs)
                ])
            return {"status": "success", "csv_data": output.getvalue()}
        except Exception as e: return {"status": "error", "message": str(e)}
