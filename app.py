from flask import Flask, render_template, request, jsonify
from backend import ExpenseManager
import threading

app = Flask(__name__)
em = ExpenseManager("cache.txt")
lock = threading.Lock() 

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/<action>', methods=['POST'])
def api_gateway(action):
    payload = request.json or {}
    
    with lock:
        try:
            # NEW: Reload data from the file
            if action == 'reload_data': return jsonify(em.reload_from_file())
            
            if action == 'get_drive_settings': return jsonify(em.get_drive_settings())
            if action == 'save_drive_settings': return jsonify(em.save_drive_settings(payload.get('enabled'), payload.get('creds'), payload.get('token')))
            if action == 'drive_push': return jsonify(em.drive_push())
            if action == 'drive_list_versions': return jsonify(em.drive_list_versions())
            if action == 'drive_preview_version': return jsonify(em.drive_preview_version(payload.get('file_id')))
            if action == 'drive_apply_version': return jsonify(em.drive_apply_version(payload.get('file_id')))
            
            if action == 'get_date_bounds': return jsonify(em.get_date_bounds())
            if action == 'get_dashboard_data': return jsonify(em.get_dashboard_data(payload.get('query', '')))
            if action == 'export_csv': return jsonify(em.export_csv(payload.get('query', '')))
            
            if action == 'add_transaction': return jsonify(em.add_transaction(payload.get('date'), payload.get('amount'), payload.get('tags')))
            if action == 'update_transaction': return jsonify(em.update_transaction(payload.get('date'), payload.get('index'), payload.get('amount'), payload.get('tags')))
            if action == 'delete_transaction': return jsonify(em.delete_transaction(payload.get('date'), payload.get('index')))
            
            if action == 'get_sources': return jsonify(em.get_sources())
            if action == 'add_source': return jsonify(em.add_source(payload.get('url')))
            if action == 'remove_source': return jsonify(em.remove_source(payload.get('url')))
            if action == 'scan_network_and_sync': return jsonify(em.scan_network_and_sync())
            if action == 'sync_all_sources': return jsonify(em.sync_all_sources())
            
            if action == 'get_chart_lines': return jsonify(em.get_chart_lines())
            if action == 'save_chart_lines': return jsonify(em.save_chart_lines(payload.get('lines')))
            
            return jsonify({"status": "error", "message": "Unknown action"}), 400
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)