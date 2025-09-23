
import os, hashlib, sqlite3, time, json, re
from pathlib import Path
from flask import Flask, request, render_template, redirect, url_for, send_from_directory, flash, jsonify

APP_DIR = Path(__file__).parent.resolve()
DATA_DIR = APP_DIR / "data"
STATIC_DIR = APP_DIR / "static"
MODEL_DIR = STATIC_DIR / "models"
UPLOAD_DIR = STATIC_DIR / "uploads"
DB_PATH = DATA_DIR / "app.db"

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(MODEL_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "dev-secret")

# --- DB helpers ---
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS generations(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        prompt TEXT,
        mode TEXT, -- 'text' or 'image'
        image_path TEXT,
        provider TEXT,
        model_path TEXT,
        status TEXT,
        duration_ms INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        cache_hit INTEGER DEFAULT 0,
        reuse_of INTEGER
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS feedback(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        generation_id INTEGER,
        rating INTEGER, -- 1..5
        issues TEXT, -- json list
        comment TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(generation_id) REFERENCES generations(id)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS prompt_cache(
        hash TEXT PRIMARY KEY,
        prompt TEXT,
        model_path TEXT,
        provider TEXT,
        quality_score REAL DEFAULT NULL, -- avg rating
        last_used TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.commit()
    conn.close()

init_db()

# --- Utility: simple OBJ stats ---
def parse_obj_stats(obj_path: Path):
    verts = faces = 0
    try:
        with open(obj_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                ls = line.strip()
                if ls.startswith("v "): verts += 1
                elif ls.startswith("f "): faces += 1
    except Exception:
        pass
    return {"vertices": verts, "faces": faces, "file_size_kb": round(obj_path.stat().st_size/1024, 2)}

# --- Provider Adapters (stubs) ---
class ProviderBase:
    slug = "base"
    def generate(self, prompt:str, image_path:str|None):
        raise NotImplementedError

class MockProvider(ProviderBase):
    slug = "mock"
    def generate(self, prompt:str, image_path:str|None):
        # Always returns the placeholder cube quickly
        t0 = time.time()
        out_path = MODEL_DIR / f"{hashlib.sha256((prompt or 'image').encode()).hexdigest()[:12]}_cube.obj"
        # copy placeholder
        src = MODEL_DIR / "placeholder_cube.obj"
        with open(src, "r", encoding="utf-8") as fin, open(out_path, "w", encoding="utf-8") as fout:
            fout.write(fin.read())
            fout.write(f"\n# prompt: {prompt}\n")
        duration_ms = int((time.time()-t0)*1000)
        return out_path, duration_ms

# Stubs for real providers (fill in with actual API calls if you have keys)
class MeshyProvider(ProviderBase):
    slug = "meshy"
    def generate(self, prompt:str, image_path:str|None):
        # TODO: Implement with Meshy API https://docs.meshy.ai/api/text-to-3d
        # For now fallback to mock
        return MockProvider().generate(prompt, image_path)

class KaedimProvider(ProviderBase):
    slug = "kaedim"
    def generate(self, prompt:str, image_path:str|None):
        # TODO: Implement webhook polling to fetch finished asset
        return MockProvider().generate(prompt, image_path)

class TripoSRProvider(ProviderBase):
    slug = "tripoSR"
    def generate(self, prompt:str, image_path:str|None):
        # TODO: If image_path provided, run local TripoSR or call a hosted endpoint
        return MockProvider().generate(prompt, image_path)

PROVIDERS = {
    "mock": MockProvider(),
    "meshy": MeshyProvider(),
    "kaedim": KaedimProvider(),
    "tripoSR": TripoSRProvider(),
}

# --- Caching & similarity ---
def normalize_prompt(p:str)->str:
    p = (p or "").strip().lower()
    p = re.sub(r"\s+", " ", p)
    return p

def prompt_hash(p:str)->str:
    return hashlib.sha256(normalize_prompt(p).encode()).hexdigest()

def find_similar_prompt(conn, p:str, threshold:float=0.9):
    # naive token overlap similarity
    tokens = set(normalize_prompt(p).split())
    if not tokens: return None
    c = conn.cursor()
    rows = c.execute("SELECT hash, prompt, model_path, quality_score FROM prompt_cache").fetchall()
    best = None
    best_score = 0.0
    for r in rows:
        tt = set(normalize_prompt(r["prompt"]).split())
        if not tt: continue
        j = len(tokens & tt) / max(1, len(tokens | tt))
        if j > best_score:
            best_score = j
            best = r
    if best and best_score >= threshold:
        return best
    return None

# --- Routes ---
@app.route("/")
def index():
    conn = get_db()
    gens = conn.execute("SELECT * FROM generations ORDER BY id DESC LIMIT 10").fetchall()
    conn.close()
    return render_template("index.html", gens=gens)

@app.route("/generate", methods=["POST"])
def generate():
    mode = request.form.get("mode", "text")
    prompt = request.form.get("prompt", "")
    provider_key = request.form.get("provider", "meshy")
    reuse_policy = request.form.get("reuse_policy", "smart") # smart|always|never

    image_path = None
    if mode == "image" and "image" in request.files and request.files["image"].filename:
        f = request.files["image"]
        ext = os.path.splitext(f.filename)[1].lower() or ".png"
        image_path = UPLOAD_DIR / f"upl_{int(time.time())}{ext}"
        f.save(image_path)

    conn = get_db()
    h = prompt_hash(prompt if mode=="text" else (prompt + f"::image@{os.path.basename(image_path) if image_path else 'none'}"))
    # Try exact cache hit
    row = conn.execute("SELECT * FROM prompt_cache WHERE hash=?", (h,)).fetchone()
    reuse_from = None
    cache_hit = 0
    model_path = None

    def record_generation(_model_path, _provider, _dur_ms, _status="ok", _cache_hit=0, _reuse_of=None):
        c = conn.cursor()
        c.execute("""INSERT INTO generations(prompt, mode, image_path, provider, model_path, status, duration_ms, cache_hit, reuse_of)
                     VALUES(?,?,?,?,?,?,?,?,?)""",
                  (prompt, mode, str(image_path) if image_path else None, _provider, str(_model_path), _status, _dur_ms, _cache_hit, _reuse_of))
        gen_id = c.lastrowid
        conn.commit()
        return gen_id

    if reuse_policy in ("always", "smart") and row:
        # Exact reuse
        model_path = Path(row["model_path"])
        cache_hit = 1
        gen_id = record_generation(model_path, row["provider"], 0, _cache_hit=1, _reuse_of=None)
        conn.close()
        flash("命中缓存：复用已有模型。", "success")
        return redirect(url_for("detail", gen_id=gen_id))

    if reuse_policy == "smart" and not row:
        sim = find_similar_prompt(conn, prompt, threshold=0.92)
        if sim and (sim["quality_score"] or 4.0) >= 3.5:
            model_path = Path(sim["model_path"])
            cache_hit = 2  # similar reuse
            gen_id = record_generation(model_path, "cache-similar", 0, _cache_hit=2, _reuse_of=None)
            conn.close()
            flash("相似提示词复用（基于历史高评分）。", "info")
            return redirect(url_for("detail", gen_id=gen_id))

    # Otherwise call provider
    provider = PROVIDERS.get(provider_key, PROVIDERS["meshy"])
    out_path, dur_ms = provider.generate(prompt, str(image_path) if image_path else None)
    gen_id = record_generation(out_path, provider.slug, dur_ms, _cache_hit=0, _reuse_of=None)

    # Update cache
    conn.execute("""INSERT OR REPLACE INTO prompt_cache(hash, prompt, model_path, provider, quality_score, last_used)
                    VALUES(?,?,?,?,COALESCE((SELECT quality_score FROM prompt_cache WHERE hash=?), NULL), CURRENT_TIMESTAMP)""",
                 (h, prompt, str(out_path), provider.slug, h))
    conn.commit()
    conn.close()
    flash("生成完成。", "success")
    return redirect(url_for("detail", gen_id=gen_id))

@app.route("/generation/<int:gen_id>")
def detail(gen_id:int):
    conn = get_db()
    g = conn.execute("SELECT * FROM generations WHERE id=?", (gen_id,)).fetchone()
    if not g:
        conn.close()
        return "Not found", 404
    stats = {}
    if g["model_path"]:
        stats = parse_obj_stats(Path(g["model_path"]))
    fb = conn.execute("SELECT * FROM feedback WHERE generation_id=? ORDER BY id DESC", (gen_id,)).fetchall()
    conn.close()
    return render_template("detail.html", g=g, stats=stats, feedbacks=fb)

@app.route("/feedback/<int:gen_id>", methods=["POST"])
def feedback(gen_id:int):
    rating = int(request.form.get("rating", "0") or 0)
    issues = request.form.getlist("issues")
    comment = request.form.get("comment", "").strip()
    conn = get_db()
    conn.execute("INSERT INTO feedback(generation_id, rating, issues, comment) VALUES(?,?,?,?)",
                 (gen_id, rating, json.dumps(issues, ensure_ascii=False), comment))
    # Update avg rating in cache if possible
    g = conn.execute("SELECT * FROM generations WHERE id=?", (gen_id,)).fetchone()
    if g:
        h = prompt_hash(g["prompt"] if g["mode"]=="text" else (g["prompt"] + f"::image@{os.path.basename(g['image_path']) if g['image_path'] else 'none'}"))
        row = conn.execute("SELECT AVG(rating) as avg_r FROM feedback WHERE generation_id IN (SELECT id FROM generations WHERE prompt=?)", (g["prompt"],)).fetchone()
        if row and row["avg_r"]:
            conn.execute("UPDATE prompt_cache SET quality_score=? WHERE hash=?", (row["avg_r"], h))
    conn.commit()
    conn.close()
    flash("感谢反馈！", "success")
    return redirect(url_for("detail", gen_id=gen_id))

@app.route("/metrics")
def metrics():
    conn = get_db()
    # Simple KPIs
    total = conn.execute("SELECT COUNT(*) as c FROM generations").fetchone()["c"]
    cache_hits = conn.execute("SELECT COUNT(*) as c FROM generations WHERE cache_hit>0").fetchone()["c"]
    avg_rating = conn.execute("SELECT AVG(rating) as a FROM feedback").fetchone()["a"]
    avg_duration = conn.execute("SELECT AVG(duration_ms) as d FROM generations WHERE duration_ms>0").fetchone()["d"]
    top_prompts = conn.execute("""SELECT prompt, COUNT(*) as n FROM generations 
                                  GROUP BY prompt ORDER BY n DESC LIMIT 10""").fetchall()
    conn.close()
    data = {
        "total_generations": total,
        "cache_hit_rate": round((cache_hits/total)*100,2) if total else 0,
        "avg_user_rating": round(avg_rating,2) if avg_rating else None,
        "avg_generation_time_ms": int(avg_duration) if avg_duration else None,
        "top_prompts": [dict(r) for r in top_prompts]
    }
    return render_template("metrics.html", data=data)

@app.route("/download/<path:filename>")
def download(filename):
    # secure: only allow under static/models
    p = (MODEL_DIR / filename).resolve()
    if not str(p).startswith(str(MODEL_DIR.resolve())) or not p.exists():
        return "Not found", 404
    return send_from_directory(MODEL_DIR, p.name, as_attachment=True)

@app.template_filter("basename")
def basename_filter(p):
    if not p: return ""
    return os.path.basename(p)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=7860, debug=True)
