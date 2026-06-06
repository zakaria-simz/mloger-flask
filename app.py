from flask import Flask, render_template, request, jsonify
from backend import ExpenseManager
import threading
import traceback

app = Flask(__name__)
em = ExpenseManager()
lock = threading.Lock() 

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/<action>', methods=['POST'])
def api_gateway(action):
    payload = request.json or {}
    
    with lock:
        try:
            acc_id = payload.get('acc_id')
            
            # Multi-Account endpoints
            if action == 'get_accounts_summary': return jsonify(em.get_accounts_summary())
            if action == 'create_account': return jsonify(em.create_account(payload.get('name'), payload.get('type')))
            if action == 'add_transfer': return jsonify(em.add_transfer(payload.get('date'), payload.get('from_acc'), payload.get('to_acc'), payload.get('amount'), payload.get('tags')))
            
            # Data retrieval
            if action == 'get_date_bounds': return jsonify(em.get_date_bounds(acc_id))
            if action == 'get_dashboard_data': return jsonify(em.get_dashboard_data(acc_id, payload.get('query', '')))
            if action == 'export_csv': return jsonify(em.export_csv(acc_id, payload.get('query', '')))
            
            # Transactions
            if action == 'add_transaction': return jsonify(em.add_transaction(acc_id, payload.get('date'), payload.get('amount'), payload.get('tags')))
            if action == 'update_transaction': return jsonify(em.update_transaction(acc_id, payload.get('date'), payload.get('index'), payload.get('amount'), payload.get('tags')))
            if action == 'delete_transaction': return jsonify(em.delete_transaction(acc_id, payload.get('date'), payload.get('index')))
            
            # Chart Lines & Settings
            if action == 'get_chart_lines': return jsonify(em.get_chart_lines())
            if action == 'save_chart_lines': return jsonify(em.save_chart_lines(payload.get('lines')))
            if action == 'reload_data': return jsonify(em.reload_all())
            
            # Drive Sync
            if action == 'get_drive_settings': return jsonify(em.get_drive_settings())
            if action == 'save_drive_settings': return jsonify(em.save_drive_settings(payload.get('enabled'), payload.get('creds'), payload.get('token')))
            if action == 'drive_push': return jsonify(em.drive_push())
            if action == 'drive_list_versions': return jsonify(em.drive_list_versions())
            if action == 'drive_preview_version': return jsonify(em.drive_preview_version(payload.get('file_id')))
            if action == 'drive_apply_version': return jsonify(em.drive_apply_version(payload.get('file_id')))
            
            return jsonify({"status": "error", "message": "Unknown action"}), 400
        except Exception as e:
            # Print exact error to server console for debugging if needed
            traceback.print_exc()
            return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)