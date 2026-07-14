#!/usr/bin/env python3
import os
import sys
import json
import subprocess
import threading
import queue

# Force UTF-8 encoding on standard output for Windows consoles
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from pathlib import Path
from flask import Flask, Response, request, render_template, jsonify, send_from_directory

app = Flask(__name__, template_folder='templates')

# Global process management
running_process = None
log_queue = queue.Queue()
process_lock = threading.Lock()

# Detect Python interpreter from the conda environment
PYTHON_BIN = sys.executable
SCRIPT_DIR = Path(__file__).resolve().parent
BUILD_SCRIPT = SCRIPT_DIR / "build.py"

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/load-project')
def load_project():
    project_path = request.args.get('path', '').strip()
    if not project_path:
        return jsonify({"success": False, "error": "Đường dẫn dự án không được để trống"}), 400
    
    p = Path(project_path).resolve()
    if not p.is_dir():
        return jsonify({"success": False, "error": "Đường dẫn không tồn tại hoặc không phải là thư mục"}), 404
        
    cpr_files = list(p.glob('*.cpr'))
    if not cpr_files:
        return jsonify({"success": False, "error": "Không tìm thấy tệp cấu hình Copre (.cpr) trong thư mục"}), 400
        
    # Check if preview or full web exists
    web_dir = p / 'web'
    preview_dir = p / 'web_preview'
    
    metadata = None
    preview_exists = False
    result_exists = False
    
    # Try web first, then preview
    for d in [web_dir, preview_dir]:
        meta_file = d / 'dom' / 'metadata.json'
        if meta_file.exists():
            try:
                metadata = json.loads(meta_file.read_text(encoding='utf-8'))
                if d == web_dir:
                    result_exists = True
                else:
                    preview_exists = True
            except Exception as e:
                print(f"Error reading metadata from {meta_file}: {e}")
                
    # Check if preview image exists specifically
    has_preview_image = False
    for d in [web_dir, preview_dir]:
        img_file = d / 'dom' / 'dom.jpg'
        if img_file.exists():
            has_preview_image = True
            if d == web_dir:
                preview_source = 'web'
            else:
                preview_source = 'web_preview'
            break
    else:
        preview_source = None

    # Load shapefile boundary if exists
    boundary_wkt = None
    shp_files = list(p.glob('Vector/Line1.shp'))
    if shp_files:
        try:
            import shapefile
            with shapefile.Reader(str(shp_files[0])) as sf:
                for shape in sf.shapes():
                    pts = shape.points
                    if len(pts) >= 3:
                        coords = ', '.join(f'{pt[0]} {pt[1]}' for pt in pts)
                        if pts[0] != pts[-1]:
                            coords += f', {pts[0][0]} {pts[0][1]}'
                        boundary_wkt = f'POLYGON(({coords}))'
                        break
        except Exception as e:
            print(f"Error loading boundary shapefile: {e}")

    # Load calibration.json if exists
    calibration = None
    cal_file = p / 'calibration.json'
    if cal_file.exists():
        try:
            calibration = json.loads(cal_file.read_text(encoding='utf-8'))
        except Exception as e:
            print(f"Error loading calibration.json: {e}")

    return jsonify({
        "success": True,
        "projectName": cpr_files[0].stem,
        "previewExists": preview_exists or result_exists,
        "resultExists": result_exists,
        "hasPreviewImage": has_preview_image,
        "previewSource": preview_source,
        "metadata": metadata,
        "boundaryWkt": boundary_wkt,
        "calibration": calibration
    })

@app.route('/api/save-calibration', methods=['POST'])
def save_calibration():
    data = request.json or {}
    project_path = data.get('project_path', '').strip()
    cal_data = data.get('calibration')
    
    if not project_path:
        # Fallback to query string or cookie
        project_path = request.args.get('path', '').strip()
    if not project_path:
        project_path = request.cookies.get('current_project_path', '').strip()
        
    if not project_path:
        return jsonify({"success": False, "error": "Thiếu đường dẫn dự án"}), 400
    if not cal_data:
        return jsonify({"success": False, "error": "Thiếu dữ liệu cân chỉnh"}), 400
        
    p = Path(project_path).resolve()
    if not p.is_dir():
        return jsonify({"success": False, "error": "Đường dẫn dự án không hợp lệ"}), 400
        
    cal_file = p / 'calibration.json'
    try:
        cal_file.write_text(json.dumps(cal_data, indent=2), encoding='utf-8')
        return jsonify({"success": True, "message": "Đã lưu cấu hình cân chỉnh thành công"})
    except Exception as e:
        return jsonify({"success": False, "error": f"Lỗi ghi tệp cấu hình: {str(e)}"}), 500


@app.route('/api/preview-image')
def preview_image():
    project_path = request.args.get('path', '').strip()
    source = request.args.get('source', 'web_preview').strip()
    
    if not project_path:
        return "Missing path", 400
        
    p = Path(project_path).resolve()
    img_dir = p / source / 'dom'
    
    if not img_dir.exists():
        img_dir = p / 'web' / 'dom' # Fallback
        
    if not (img_dir / 'dom.jpg').exists():
        return "Image not found", 404
        
    return send_from_directory(str(img_dir), 'dom.jpg')

def run_build_subprocess(cmd, project_path):
    global running_process, log_queue
    try:
        # Empty queue
        while not log_queue.empty():
            try:
                log_queue.get_nowait()
            except queue.Empty:
                break
                
        log_queue.put("[SYS] Bắt đầu tiến trình: " + " ".join(cmd) + "\n")
        
        # Start subprocess with UTF-8 encoding environment
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'
        running_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            bufsize=1,
            env=env
        )
        
        # Read output line by line
        for line in running_process.stdout:
            log_queue.put(line)
            
        running_process.wait()
        log_queue.put(f"\n[SYS] Tiến trình hoàn thành với mã thoát: {running_process.returncode}\n")
    except Exception as e:
        log_queue.put(f"\n[SYS] LỖI HỆ THỐNG: {str(e)}\n")
    finally:
        running_process = None
        log_queue.put(None) # Signal end of stream

@app.route('/api/start-build', methods=['POST'])
def start_build():
    global running_process
    
    data = request.json or {}
    project_path = data.get('project_path', '').strip()
    action = data.get('action', 'build').strip() # 'preview' or 'build'
    
    if not project_path:
        return jsonify({"success": False, "error": "Thiếu đường dẫn dự án"}), 400
        
    p = Path(project_path).resolve()
    if not p.exists():
        return jsonify({"success": False, "error": "Đường dẫn dự án không tồn tại"}), 400

    with process_lock:
        if running_process is not None:
            return jsonify({"success": False, "error": "Một tiến trình xử lý khác đang chạy. Hãy đợi hoặc Huỷ tiến trình đó trước."}), 409
            
        # Build command list
        cmd = [PYTHON_BIN, str(BUILD_SCRIPT), str(p)]
        
        if action == 'preview':
            cmd.append('--preview')
        else:
            # Clip parameters
            clip_wkt = data.get('clip_wkt', '').strip()
            if clip_wkt:
                cmd.extend(['--clip-wkt', clip_wkt])
                
            # Quality step
            step = data.get('pointcloud_step', 1)
            cmd.extend(['--pointcloud-step', str(step)])
            
            # Skip flags
            if data.get('skip_pointcloud'):
                cmd.append('--skip-pointcloud')
            if data.get('skip_model'):
                cmd.append('--skip-model')
            if data.get('skip_dom'):
                cmd.append('--skip-dom')
                
            # Compression options
            if data.get('compress_model'):
                cmd.append('--compress-model')
            
            simplify = data.get('simplify_ratio')
            if simplify is not None and simplify != '':
                cmd.extend(['--simplify-ratio', str(simplify)])

                
        # Run in separate thread
        thread = threading.Thread(target=run_build_subprocess, args=(cmd, project_path))
        thread.start()
        
    return jsonify({"success": True, "message": "Đã khởi chạy tiến trình thành công"})

@app.route('/api/cancel-build', methods=['POST'])
def cancel_build():
    global running_process
    with process_lock:
        if running_process is None:
            return jsonify({"success": False, "error": "Không có tiến trình nào đang chạy"}), 400
            
        try:
            running_process.terminate()
            log_queue.put("\n[SYS] TIẾN TRÌNH ĐÃ BỊ YÊU CẦU HUỶ BỞI NGƯỜI DÙNG.\n")
            return jsonify({"success": True, "message": "Đã gửi yêu cầu dừng tiến trình"})
        except Exception as e:
            return jsonify({"success": False, "error": f"Không thể dừng tiến trình: {str(e)}"}), 500

@app.route('/api/stream-logs')
def stream_logs():
    def generate():
        while True:
            line = log_queue.get()
            if line is None:
                yield "data: [DONE]\n\n"
                break
            # Send message format in SSE
            yield f"data: {json.dumps(line)}\n\n"
    return Response(generate(), mimetype='text/event-stream')

# Serve static files from the project output web folder
@app.route('/results/<path:filename>')
def serve_results(filename):
    project_path = request.args.get('path', '').strip()
    
    # Fallback to cookie if query parameter is not present
    if not project_path:
        project_path = request.cookies.get('current_project_path', '').strip()
        
    if not project_path:
        return "Missing project path parameter or cookie", 400
    
    p = Path(project_path).resolve()
    
    # Intercept calibration.json to serve it from the project root instead of web/
    if filename == 'calibration.json':
        cal_path = p / 'calibration.json'
        if cal_path.exists():
            return send_from_directory(str(p), 'calibration.json')
            
    web_dir = p / 'web'
    
    if not web_dir.is_dir():
        return f"Result folder not found at {web_dir}", 404
        
    # Serve index.html or other files
    response = send_from_directory(str(web_dir), filename)
    
    # Set the cookie if the path was provided in the query string
    if request.args.get('path'):
        response.set_cookie('current_project_path', project_path, path='/results')
        
    return response

if __name__ == '__main__':
    print("--------------------------------------------------")
    print("  KHOI CHAY GIAO DIEN CAT VUON UOM 3D")
    print("  Truy cap: http://localhost:5000")
    print("--------------------------------------------------")
    app.run(host='0.0.0.0', port=5000, debug=True)
