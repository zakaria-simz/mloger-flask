import re
import os
import json
import shutil
import socket
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

def is_transfer_transaction(t: Transaction) -> bool:
    for tag in t.tags:
        tl = tag.lower().strip()
        if tl in ['transfer', 'loan'] or tl.startswith('to ') or tl.startswith('from '):
            return True
    return False

@dataclass
class DailyRecord:
    date: date
    balance_snapshot: Optional[float] = None
    transactions: List[Transaction] = field(default_factory=list)

    @property
    def total_change(self) -> float: 
        return sum(t.amount for t in self.transactions)
        
    @property
    def income(self) -> float: 
        return sum(t.amount for t in self.transactions if t.amount > 0 and not is_transfer_transaction(t))
        
    @property
    def expense(self) -> float: 
        return sum(t.amount for t in self.transactions if t.amount < 0 and not is_transfer_transaction(t))

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
        if not query or not query.strip() or "..." in query: return None
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
        name = f"DB_Dump_{dt.now().strftime('%Y-%m-%d_%H-%M-%S')}.json"
        file_metadata = {'name': name, 'parents': [folder_id]}
        media = MediaFileUpload(filepath, mimetype='application/json')
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
    def __init__(self):
        self.records = {}
        self.query_engine = QueryParser()
        self.settings = self._load_settings()
        
        if not os.path.exists(BACKUP_DIR): os.makedirs(BACKUP_DIR)
        self.reload_all()
        
        self.drive_mgr = None
        if self.settings.get('drive_enabled'): self._init_drive()

    def _load_settings(self):
        settings = {"accounts": {}, "chart_lines": {"balance": [], "main": [], "analytics": []}, "drive_enabled": False, "fill_empty_days": False}
        if os.path.exists(SETTINGS_FILE):
            try: 
                with open(SETTINGS_FILE, 'r') as f: 
                    loaded = json.load(f)
                    settings.update(loaded)
            except: pass
        
        if not settings["accounts"]:
            settings["accounts"]["main"] = {"name": "Main Wallet", "type": "wallet", "file": "cache.txt"}
            self._save_settings_raw(settings)
            
        return settings

    def _save_settings_raw(self, data):
        with open(SETTINGS_FILE, 'w') as f: json.dump(data, f, indent=4)
        
    def _save_settings(self):
        self._save_settings_raw(self.settings)

    def reload_all(self):
        self.records = {}
        for acc_id, acc_info in self.settings["accounts"].items():
            self.records[acc_id] = self._read_file(acc_info["file"])
        return {"status": "success"}

    def _read_file(self, filepath) -> Dict[date, DailyRecord]:
        recs = {}
        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    r = self._parse_line(line)
                    if r: recs[r.date] = r
        return recs

    def save_to_file(self, acc_id, backup=True):
        if acc_id not in self.settings["accounts"]: return {"status": "error"}
        filepath = self.settings["accounts"][acc_id]["file"]
        
        if backup and os.path.exists(filepath):
            timestamp = dt.now().strftime("%Y%m%d_%H%M%S")
            shutil.copy(filepath, os.path.join(BACKUP_DIR, f"{acc_id}_{timestamp}.txt"))
            
        with open(filepath, "w", encoding="utf-8") as f:
            for rec in sorted(self.records.get(acc_id, {}).values(), key=lambda r: r.date):
                f.write(rec.to_file_line() + "\n")
        return {"status": "success"}

    def toggle_empty_days(self):
        self.settings["fill_empty_days"] = not self.settings.get("fill_empty_days", False)
        self._save_settings()
        return {"status": "success", "fill_empty_days": self.settings["fill_empty_days"]}

    def get_accounts_summary(self):
        summary = {}
        for acc_id, acc_info in self.settings["accounts"].items():
            recs = self.records.get(acc_id, {})
            bal = sum(r.total_change for r in recs.values())
            summary[acc_id] = {
                "name": acc_info["name"],
                "type": acc_info["type"],
                "balance": round(bal, 2)
            }
        return {"status": "success", "accounts": summary}

    def create_account(self, name: str, acc_type: str):
        if not name or not name.strip(): return {"status": "error", "message": "Invalid name"}
        acc_id = re.sub(r'[^a-zA-Z0-9]', '', name).lower() + str(int(dt.now().timestamp()))
        filename = f"acc_{acc_id}.txt"
        
        self.settings["accounts"][acc_id] = {"name": name.strip(), "type": acc_type, "file": filename}
        self.records[acc_id] = {}
        self.save_to_file(acc_id, backup=False)
        self._save_settings()
        return {"status": "success", "account_id": acc_id}

    def add_transfer(self, date_str: str, from_acc: str, to_acc: str, amount: float, tags: List[str]):
        try:
            amt = abs(float(amount))
            if amt == 0: return {"status": "error", "message": "Amount must be > 0"}
            from_name = self.settings['accounts'][from_acc]['name']
            to_name = self.settings['accounts'][to_acc]['name']
            
            transfer_id = str(int(dt.now().timestamp()))
            transfer_tags = [t for t in tags if t.lower() != 'transfer'] + ["Transfer", f"TID:{transfer_id}"]
            
            self.add_transaction(from_acc, date_str, -amt, transfer_tags + [f"To {to_name}"])
            self.add_transaction(to_acc, date_str, amt, transfer_tags + [f"From {from_name}"])
            return {"status": "success"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

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
            dump = {"settings": self.settings, "files": {}}
            for acc in self.settings['accounts'].values():
                if os.path.exists(acc['file']):
                    with open(acc['file'], 'r', encoding='utf-8') as f:
                        dump["files"][acc['file']] = f.read()
            
            temp_path = f"temp_sync_{dt.now().timestamp()}.json"
            with open(temp_path, 'w', encoding='utf-8') as f: json.dump(dump, f)
            
            fid, name = self.drive_mgr.upload_file(temp_path)
            os.remove(temp_path)
            
            self.settings['last_synced'] = dt.now().strftime("%Y-%m-%d %H:%M:%S")
            self._save_settings()
            return {"status": "success", "message": f"Database Backup Uploaded", "last_synced": self.settings['last_synced']}
        except Exception as e: return {"status": "error", "message": str(e)}

    def drive_list_versions(self):
        if not self.drive_mgr: return {"status": "error", "message": "Drive not enabled"}
        try: return {"status": "success", "files": self.drive_mgr.list_versions()}
        except Exception as e: return {"status": "error", "message": str(e)}

    def drive_preview_version(self, file_id):
        if not self.drive_mgr: return {"status": "error", "message": "Drive not enabled"}
        try:
            content = self.drive_mgr.download_content(file_id)
            try:
                data = json.loads(content)
                accs = len(data.get("accounts", {}))
                return {"status": "success", "preview": f"Complete Multi-Account DB Dump\nAccounts: {accs}"}
            except:
                lines = content.splitlines()
                return {"status": "success", "preview": f"Legacy Single-File Backup\nFirst 3 lines:\n" + "\n".join(lines[:3])}
        except Exception as e: return {"status": "error", "message": str(e)}

    def drive_apply_version(self, file_id):
        if not self.drive_mgr: return {"status": "error", "message": "Drive not enabled"}
        try:
            content = self.drive_mgr.download_content(file_id)
            try:
                data = json.loads(content)
                self.settings = data["settings"]
                self._save_settings()
                for filename, fcontent in data.get("files", {}).items():
                    with open(filename, 'w', encoding='utf-8') as f: f.write(fcontent)
            except json.JSONDecodeError:
                with open("cache.txt", 'w', encoding='utf-8') as f: f.write(content)
                
            self.reload_all()
            self.settings['last_synced'] = dt.now().strftime("%Y-%m-%d %H:%M:%S")
            self._save_settings()
            return {"status": "success", "message": "Backup Applied!"}
        except Exception as e: return {"status": "error", "message": str(e)}

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

    def update_transaction(self, acc_id: str, date_str: str, trans_index: int, amount: float, tags: List[str]):
        try:
            if acc_id not in self.records: return {"status": "error", "message": "Account missing"}
            target_date = dt.strptime(date_str, TIME_FORMAT).date()
            if target_date not in self.records[acc_id]: return {"status": "error", "message": "Date not found"}
            record = self.records[acc_id][target_date]
            if trans_index < 0 or trans_index >= len(record.transactions): return {"status": "error", "message": "Idx out of bounds"}
            record.transactions[trans_index] = Transaction(amount, tags)
            self.save_to_file(acc_id)
            return {"status": "success"}
        except Exception as e: return {"status": "error", "message": str(e)}

    def add_transaction(self, acc_id: str, date_str: str, amount: float, tags: List[str]):
        try:
            if acc_id not in self.records: return {"status": "error", "message": "Account missing"}
            target_date = dt.strptime(date_str, TIME_FORMAT).date()
            if target_date not in self.records[acc_id]: self.records[acc_id][target_date] = DailyRecord(target_date, None, [])
            self.records[acc_id][target_date].transactions.append(Transaction(amount, tags))
            self.save_to_file(acc_id)
            return {"status": "success"}
        except Exception as e: return {"status": "error", "message": str(e)}
            
    def delete_transaction(self, acc_id: str, date_str: str, trans_index: int):
        try:
            if acc_id not in self.records: return {"status": "error", "message": "Account missing"}
            target_date = dt.strptime(date_str, TIME_FORMAT).date()
            if target_date not in self.records[acc_id]: return {"status": "error", "message": "Record not found"}
            
            transaction = self.records[acc_id][target_date].transactions[trans_index]
            transfer_id_tag = next((t for t in transaction.tags if t.startswith("TID:")), None)
            
            self.records[acc_id][target_date].transactions.pop(trans_index)
            self.save_to_file(acc_id)
            
            if transfer_id_tag:
                for target_acc_id, acc_recs in self.records.items():
                    if target_acc_id == acc_id: continue 
                    for d, rec in acc_recs.items():
                        for i, t in enumerate(rec.transactions):
                            if transfer_id_tag in t.tags:
                                rec.transactions.pop(i)
                                self.save_to_file(target_acc_id)
                                break
            
            return {"status": "success"}
        except Exception as e: return {"status": "error", "message": str(e)}

    def get_chart_lines(self): return self.settings.get("chart_lines", {"balance": [], "main": [], "analytics": []})
    
    def save_chart_lines(self, lines_data):
        self.settings["chart_lines"] = lines_data
        self._save_settings()
        return {"status": "success"}

    def get_date_bounds(self, acc_id: str) -> Dict[str, Any]:
        recs = self.records.get(acc_id, {})
        if not recs:
            today = date.today().strftime(TIME_FORMAT)
            return {"status": "success", "start": today, "end": today, "total_days": 0}
        sorted_dates = sorted(recs.keys())
        start_date = sorted_dates[0]
        end_date = max(sorted_dates[-1], date.today()) 
        return {
            "status": "success",
            "start": start_date.strftime(TIME_FORMAT),
            "end": end_date.strftime(TIME_FORMAT),
            "total_days": (end_date - start_date).days
        }

    # --- ADVANCED FILTERING: Constraints ---
    def _get_filtered_records(self, acc_id: str, filter_query: str, start_bound: str = None, end_bound: str = None) -> List[Tuple[DailyRecord, float]]:
        recs = self.records.get(acc_id, {})
        if not recs: return []

        sorted_recs = sorted(recs.values(), key=lambda r: r.date)
        day_balances = {}
        running_bal = 0
        
        for rec in sorted_recs:
            running_bal += rec.total_change
            day_balances[rec.date] = running_bal

        # Process Explicit User Date Bounds
        start_d = None
        end_d = None
        if start_bound and end_bound:
            try:
                start_d = dt.strptime(start_bound, TIME_FORMAT).date()
                end_d = dt.strptime(end_bound, TIME_FORMAT).date()
            except: pass
            
        if not start_d: start_d = sorted_recs[0].date
        if not end_d: end_d = max(sorted_recs[-1].date, date.today())

        result = []
        compiled_q = self.query_engine.compile_query(filter_query)
        fill_empty = self.settings.get("fill_empty_days", False)

        if fill_empty:
            curr_date = start_d
            
            # Find the running balance accurately before our starting point
            last_bal = 0
            for d in sorted((k for k in day_balances.keys() if k < start_d), reverse=True):
                last_bal = day_balances[d]
                break

            while curr_date <= end_d:
                rec = recs.get(curr_date)
                if rec:
                    last_bal = day_balances[curr_date]

                filtered_trans = []
                if rec:
                    if compiled_q is None:
                        filtered_trans = rec.transactions
                    else:
                        for t in rec.transactions:
                            if self.query_engine.evaluate(t, curr_date, compiled_q):
                                filtered_trans.append(t)

                temp_rec = DailyRecord(
                    date=curr_date, 
                    balance_snapshot=rec.balance_snapshot if rec else None, 
                    transactions=filtered_trans
                )
                result.append((temp_rec, last_bal))
                curr_date += timedelta(days=1)
        else:
            for rec in sorted_recs:
                # Apply explicit bounds if requested
                if start_d and end_d:
                    if rec.date < start_d or rec.date > end_d:
                        continue

                filtered_trans = []
                if compiled_q is None: 
                    filtered_trans = rec.transactions
                else:
                    for t in rec.transactions:
                        if self.query_engine.evaluate(t, rec.date, compiled_q): 
                            filtered_trans.append(t)
                
                if filtered_trans or (not filter_query.strip() and not rec.transactions):
                    result.append((DailyRecord(rec.date, rec.balance_snapshot, filtered_trans), day_balances[rec.date]))
                    
        return result

    def get_dashboard_data(self, acc_id: str, filter_query: str = "", start_bound: str = None, end_bound: str = None) -> Dict[str, Any]:
        if acc_id not in self.settings.get("accounts", {}): return {"status": "error", "message": "Unknown account"}
        try:
            filtered = self._get_filtered_records(acc_id, filter_query, start_bound, end_bound)
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
                    if not is_transfer_transaction(t):
                        if t.amount > 0: 
                            total_income += t.amount
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
            current_balance = round(sum(r.total_change for r in self.records.get(acc_id, {}).values()), 2)
            
            return {
                "status": "success",
                "account_info": self.settings["accounts"][acc_id],
                "records": records_out,
                "stats": stats_out,
                "global_balance": current_balance
            }
        except Exception as e:
            return {"status": "error", "message": f"Data process error: {str(e)}"}

    def export_csv(self, acc_id: str, filter_query: str = "", start_bound: str = None, end_bound: str = None) -> dict:
        try:
            filtered = self._get_filtered_records(acc_id, filter_query, start_bound, end_bound)
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