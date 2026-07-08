"""
Research Review GUI — Flask app
- SSE for live progress
- SQLite for session history
- Markdown + PDF download
"""

import os
import sqlite3
import json
import queue
import threading
import time
import re
from datetime import datetime
from pathlib import Path

from flask import (
    Flask, render_template, request, jsonify,
    Response, send_file, abort
)

app = Flask(__name__)
app.config["SECRET_KEY"] = "research-gui-secret"

# ── paths ────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
OUTPUTS_DIR = BASE_DIR / "outputs"
DB_PATH = BASE_DIR / "history.db"
OUTPUTS_DIR.mkdir(exist_ok=True)

# ── active jobs: job_id → Queue ──────────────────────────────────────────────
_job_queues: dict[str, queue.Queue] = {}
_job_lock = threading.Lock()


# ── SQLite helpers ────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id      TEXT    NOT NULL UNIQUE,
                topic       TEXT    NOT NULL,
                status      TEXT    NOT NULL DEFAULT 'running',
                created_at  TEXT    NOT NULL,
                finished_at TEXT,
                md_path     TEXT,
                pdf_path    TEXT,
                error       TEXT
            )
        """)
        db.commit()


init_db()


# ── PDF generation (markdown → PDF via weasyprint) ───────────────────────────
def md_to_pdf(md_text: str, pdf_path: str):
    try:
        import markdown
        from weasyprint import HTML, CSS

        html_body = markdown.markdown(
            md_text,
            extensions=["tables", "fenced_code", "toc"]
        )

        full_html = f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{
    font-family: 'Georgia', serif;
    font-size: 11pt;
    line-height: 1.7;
    color: #1a1a2e;
    max-width: 750px;
    margin: 0 auto;
    padding: 40px 50px;
  }}
  h1 {{ font-size: 22pt; color: #0f0f23; border-bottom: 3px solid #4f46e5; padding-bottom: 10px; }}
  h2 {{ font-size: 16pt; color: #1e1b4b; margin-top: 30px; border-bottom: 1px solid #e5e7eb; padding-bottom: 6px; }}
  h3 {{ font-size: 13pt; color: #312e81; }}
  table {{ border-collapse: collapse; width: 100%; margin: 16px 0; font-size: 9pt; }}
  th {{ background: #4f46e5; color: white; padding: 8px 10px; text-align: left; }}
  td {{ padding: 7px 10px; border: 1px solid #d1d5db; }}
  tr:nth-child(even) {{ background: #f5f3ff; }}
  code {{ background: #f1f0ff; padding: 2px 5px; border-radius: 3px; font-size: 9pt; }}
  pre {{ background: #1e1b4b; color: #e9d5ff; padding: 14px; border-radius: 6px; overflow-x: auto; }}
  blockquote {{ border-left: 4px solid #6366f1; margin: 0; padding: 0 16px; color: #4b5563; }}
  a {{ color: #4f46e5; }}
  @page {{ margin: 2cm; }}
</style>
</head>
<body>
{html_body}
</body>
</html>"""

        HTML(string=full_html).write_pdf(pdf_path)
        return True
    except Exception as e:
        print(f"PDF generation error: {e}")
        return False


# ── routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/history")
def api_history():
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM reports ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.get_json(force=True)
    topic = (data.get("topic") or "").strip()
    if not topic:
        return jsonify({"error": "Topic is required"}), 400

    import uuid
    job_id = str(uuid.uuid4())

    # register in DB
    with get_db() as db:
        db.execute(
            "INSERT INTO reports (job_id, topic, status, created_at) VALUES (?,?,?,?)",
            (job_id, topic, "running", datetime.now().isoformat())
        )
        db.commit()

    # create SSE queue for this job
    q: queue.Queue = queue.Queue()
    with _job_lock:
        _job_queues[job_id] = q

    # run pipeline in background thread
    def run():
        from main3 import run_pipeline

        def progress(msg, level="info"):
            q.put({"type": "log", "msg": msg, "level": level, "ts": datetime.now().strftime("%H:%M:%S")})

        result = run_pipeline(topic, progress_fn=progress)

        # save outputs
        safe = re.sub(r'[^\w\s-]', '', topic[:50]).strip().replace(' ', '_')
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        md_path = str(OUTPUTS_DIR / f"{safe}_{ts}.md")
        pdf_path = str(OUTPUTS_DIR / f"{safe}_{ts}.pdf")

        if result["final_ans"]:
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(result["final_ans"])
            progress(f"💾 Markdown saved.", "info")

            progress("🖨️  Generating PDF...", "info")
            ok = md_to_pdf(result["final_ans"], pdf_path)
            if ok:
                progress("✅ PDF ready for download.", "success")
            else:
                pdf_path = None
                progress("⚠️  PDF generation failed — Markdown still available.", "warn")

            with get_db() as db:
                db.execute(
                    """UPDATE reports
                       SET status='done', finished_at=?, md_path=?, pdf_path=?, error=NULL
                       WHERE job_id=?""",
                    (datetime.now().isoformat(), md_path, pdf_path, job_id)
                )
                db.commit()

            q.put({"type": "done", "job_id": job_id, "md_path": md_path, "pdf_path": pdf_path})
        else:
            err = result.get("error", "Unknown error")
            with get_db() as db:
                db.execute(
                    "UPDATE reports SET status='error', finished_at=?, error=? WHERE job_id=?",
                    (datetime.now().isoformat(), err, job_id)
                )
                db.commit()
            q.put({"type": "error", "msg": err})

        # sentinel
        q.put(None)

    t = threading.Thread(target=run, daemon=True)
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/api/stream/<job_id>")
def api_stream(job_id):
    with _job_lock:
        q = _job_queues.get(job_id)

    if q is None:
        # job may have been cleaned up; check DB
        with get_db() as db:
            row = db.execute("SELECT * FROM reports WHERE job_id=?", (job_id,)).fetchone()
        if row and row["status"] == "done":
            def done_stream():
                yield f"data: {json.dumps({'type':'done','job_id':job_id,'md_path':row['md_path'],'pdf_path':row['pdf_path']})}\n\n"
            return Response(done_stream(), mimetype="text/event-stream")
        abort(404)

    def generate():
        while True:
            try:
                item = q.get(timeout=30)
            except queue.Empty:
                yield f"data: {json.dumps({'type':'ping'})}\n\n"
                continue

            if item is None:
                break
            yield f"data: {json.dumps(item)}\n\n"

        with _job_lock:
            _job_queues.pop(job_id, None)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/download/<job_id>/<fmt>")
def api_download(job_id, fmt):
    with get_db() as db:
        row = db.execute("SELECT * FROM reports WHERE job_id=?", (job_id,)).fetchone()
    if not row:
        abort(404)

    if fmt == "md" and row["md_path"] and os.path.exists(row["md_path"]):
        return send_file(row["md_path"], as_attachment=True,
                         download_name=f"review_{job_id[:8]}.md",
                         mimetype="text/markdown")
    elif fmt == "pdf" and row["pdf_path"] and os.path.exists(row["pdf_path"]):
        return send_file(row["pdf_path"], as_attachment=True,
                         download_name=f"review_{job_id[:8]}.pdf",
                         mimetype="application/pdf")
    else:
        abort(404)


@app.route("/api/report/<job_id>")
def api_report_content(job_id):
    with get_db() as db:
        row = db.execute("SELECT * FROM reports WHERE job_id=?", (job_id,)).fetchone()
    if not row or not row["md_path"] or not os.path.exists(row["md_path"]):
        abort(404)
    with open(row["md_path"], "r", encoding="utf-8") as f:
        content = f.read()
    return jsonify({"content": content, "topic": row["topic"]})


if __name__ == "__main__":
    print("\n🚀  Research Review GUI starting at http://localhost:8000\n")
    app.run(host="0.0.0.0", port=8000, debug=False, threaded=True)
