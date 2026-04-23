import os
import sqlite3
import hashlib
import uuid
import requests
import traceback
import logging

from urllib.parse import quote
from requests.adapters import HTTPAdapter, Retry
from flask import Flask, request, redirect, session, send_from_directory
from werkzeug.utils import secure_filename
from PIL import Image, ImageDraw
import imagehash

app = Flask(__name__)
app.secret_key = "ARTGUARD_FINAL"

UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ================= DATABASE =================
def get_db():
    conn = sqlite3.connect("artguard.db", timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row  # 🔥 safer access
    return conn


def column_exists(cur, table, column):
    cur.execute(f"PRAGMA table_info({table})")
    return column in [c[1] for c in cur.fetchall()]


def init_db():
    with get_db() as conn:
        cur = conn.cursor()

        # ================= USERS =================
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE,
            phone TEXT,
            password TEXT)
        """)

        # ================= IMAGES =================
        cur.execute("""
        CREATE TABLE IF NOT EXISTS images(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user TEXT,
            filename TEXT,
            visibility TEXT,
            unique_id TEXT,
            image_hash TEXT)
        """)

        # ================= COMMENTS =================
        cur.execute("""
        CREATE TABLE IF NOT EXISTS comments(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            image_id INTEGER,
            user TEXT,
            comment TEXT)
        """)

        # ================= FOLLOWS =================
        cur.execute("""
        CREATE TABLE IF NOT EXISTS follows(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            follower TEXT,
            following TEXT
        )
        """)

        # 🔥 SAFE MIGRATION (adds column only if missing)
        if not column_exists(cur, "follows", "status"):
            cur.execute("ALTER TABLE follows ADD COLUMN status TEXT DEFAULT 'accepted'")
            print("✅ Added 'status' column safely")

        # ================= LIKES =================
        cur.execute("""
        CREATE TABLE IF NOT EXISTS likes(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            image_id INTEGER,
            user TEXT)
        """)

        conn.commit()
init_db()

# ================= HELPERS =================
def hash_password(p):
    return hashlib.sha256(p.strip().encode()).hexdigest()

def uid():
    return "ART-" + uuid.uuid4().hex[:6].upper()

def logged():
    return "user" in session

def user_folder():
    path = os.path.join(UPLOAD_FOLDER, session["user"])
    os.makedirs(path, exist_ok=True)
    return path

# ================= HASH =================
def get_hash(path):
    try:
        with Image.open(path) as img:
            return str(imagehash.phash(img.convert("RGB")))
    except:
        return "invalid"

def is_duplicate(new_hash):
    if new_hash == "invalid":
        return False

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT image_hash FROM images")
        rows = cur.fetchall()

    for r in rows:
        try:
            old = r[0]
            if old == "invalid":
                continue
            diff = imagehash.hex_to_hash(old) - imagehash.hex_to_hash(new_hash)
            if diff <= 4:
                return True
        except:
            continue
    return False

# ================= WATERMARK =================
def add_watermark(path, text):
    try:
        with Image.open(path).convert("RGBA") as img:
            txt = Image.new("RGBA", img.size, (255,255,255,0))
            draw = ImageDraw.Draw(txt)

            w,h = img.size
            draw.text((w//3, h//2), text, fill=(255,0,0,150))

            Image.alpha_composite(img, txt).convert("RGB").save(path)
    except:
        pass

# ================= PROMPT GENERATOR =================
def prompt_generator(text):
    text = text.strip().lower()

    style_map = {
        "rain": "rain droplets, cinematic lighting, wet reflections",
        "night": "night scene, neon glow, dramatic lighting",
        "girl": "beautiful subject, detailed face, soft skin texture",
        "boy": "detailed portrait, cinematic focus",
        "forest": "lush greenery, volumetric lighting, ultra detailed nature",
        "city": "futuristic cityscape, neon lights, skyscrapers",
        "mountain": "majestic mountains, fog, golden hour",
        "fire": "dramatic fire lighting, high contrast",
        "water": "flowing water, reflections, hyper realistic"
    }

    enhanced = [v for k, v in style_map.items() if k in text]
    base = text.replace(",", " ")

    return f"{base}, {', '.join(enhanced)}, ultra detailed, cinematic lighting, 4k"

# ================= NEGATIVE =================
NEGATIVE_PROMPT = "low quality, blurry, distorted, bad anatomy, watermark, text"

# ================= AI GENERATOR =================
def generate_ai(prompt, style="artistic"):
    if not prompt.strip():
        return {"type": "error", "filename": ""}

    folder = user_folder()
    filename = f"{uuid.uuid4().hex}.png"
    path = os.path.join(folder, filename)

    full_prompt = f"{prompt}, {style}, ultra detailed, 4k, {NEGATIVE_PROMPT}"
    safe_prompt = quote(full_prompt)

    urls = [
        f"https://image.pollinations.ai/prompt/{safe_prompt}?width=512&height=512&seed={uuid.uuid4().hex}",
        f"https://image.pollinations.ai/prompt/{safe_prompt}"
    ]

    req_session = requests.Session()

    retries = Retry(total=3, backoff_factor=1,
                    status_forcelist=[500, 502, 503, 504])
    req_session.mount("https://", HTTPAdapter(max_retries=retries))

    headers = {"User-Agent": "Mozilla/5.0"}

    for url in urls:
        for _ in range(3):
            try:
                r = req_session.get(url, headers=headers, timeout=25)

                if r.status_code != 200 or len(r.content) < 2000:
                    continue

                tmp_path = path + ".tmp"
                with open(tmp_path, "wb") as f:
                    f.write(r.content)

                try:
                    with Image.open(tmp_path) as img:
                        img.verify()
                except:
                    os.remove(tmp_path)
                    continue

                os.rename(tmp_path, path)

                code = uid()
                add_watermark(path, code)
                h = get_hash(path)

                if is_duplicate(h):
                    os.remove(path)
                    return {"type": "duplicate"}

                with get_db() as conn:
                    cur = conn.cursor()
                    cur.execute("""
                        INSERT INTO images(user, filename, visibility, unique_id, image_hash)
                        VALUES(?,?,?,?,?)
                    """, (session["user"], filename, "private", code, h))
                    conn.commit()

                return {"type": "image", "filename": filename}

            except Exception as e:
                logging.error(f"Retry error: {e}")
                continue

    # ❌ FINAL FAIL
    filename = f"{uuid.uuid4().hex}_error.png"
    path = os.path.join(folder, filename)

    img = Image.new("RGB", (512, 512), (40, 40, 60))
    draw = ImageDraw.Draw(img)
    draw.text((120, 250), "AI FAILED", fill=(255,255,255))
    img.save(path)

    return {"type": "error", "filename": filename}
# ================= UI =================
UI = """
<meta charset="UTF-8">
<style>
body{margin:0;font-family:Segoe UI;background:linear-gradient(135deg,#020617,#0f172a);color:white;}
.top{background:#020617;padding:15px;color:#38bdf8;font-size:22px;font-weight:bold;}
.side{position:fixed;width:230px;height:100%;background:#020617;padding:20px;}
.side a{display:block;padding:12px;margin:10px 0;color:white;text-decoration:none;border-radius:10px;}
.side a:hover{background:#38bdf8;color:black;}
.main{margin-left:250px;padding:30px;}
.card{background:rgba(255,255,255,0.05);padding:20px;border-radius:15px;margin-bottom:20px;}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:20px;}
input,select{width:100%;padding:12px;margin:10px 0;background:#111827;color:white;border-radius:8px;border:1px solid #38bdf8;}
button{width:100%;padding:12px;background:#38bdf8;border:none;border-radius:8px;font-weight:bold;}
.msg{padding:10px;border-radius:8px;margin-top:10px;}
.success{background:#065f46;}
.error{background:#7f1d1d;}
img{width:100%;border-radius:10px;}
</style>
<div class='top'>🎨 ArtGuard</div>
"""

def sidebar():
    return """
    <div class='side'>
    <a href='/home'>🏠 Home</a>
    <a href='/generate'>✨ Generate</a>
    <a href='/prompt'>🧠 Prompt Generator</a>
    <a href='/users'>👥 Users</a>
    <a href='/requests'>📩 Requests</a>
    <a href='/profile'>👤 Profile</a>
    <a href='/upload'>⬆ Upload</a>
    <a href='/gallery'>🔒 Private</a>
    <a href='/public'>🌐 Public</a>
    <a href='/logout'>🚪 Logout</a>
    </div>
    """

# ================= ROUTES =================
from collections import defaultdict

def prompt_generator(text):
    text = text.lower().strip()
    words = text.split()

    scores = defaultdict(float)

    # ---------------- SUBJECT MAP (+20 expanded) ----------------
    subject_map = {
        "baby": 3, "child": 3, "boy": 2.5, "girl": 2.5,
        "man": 2, "woman": 2, "person": 2,
        "dog": 2, "cat": 2, "animal": 2,
        "lion": 3, "tiger": 3, "horse": 2.5,
        "robot": 2.5, "angel": 3, "demon": 3,

        # +20 NEW
        "queen": 2.5, "king": 2.5, "warrior": 3,
        "knight": 3, "soldier": 2.5, "wizard": 3,
        "witch": 3, "fairy": 3, "princess": 3,
        "prince": 2.5, "hunter": 2.5, "childhood": 2,
        "elder": 2, "oldman": 2, "oldwoman": 2,
        "alien": 3, "monster": 3, "hero": 2.5,
        "villain": 2.5, "student": 2
    }

    # ---------------- ACTION MAP (+20 expanded) ----------------
    action_map = {
        "sleep": 3, "sleeping": 3, "run": 3, "running": 3,
        "dance": 3, "dancing": 3, "sit": 2,
        "cry": 2.5, "smile": 2.5,
        "fight": 3, "battle": 3,
        "fly": 3, "flying": 3,
        "walk": 2, "standing": 2,

        # +20 NEW
        "jump": 3, "jumping": 3,
        "laugh": 2.5, "laughing": 2.5,
        "scream": 2.5, "shout": 2.5,
        "reading": 2, "write": 2,
        "painting": 3, "drawing": 3,
        "swimming": 3, "drowning": 3,
        "driving": 2.5, "riding": 2.5,
        "thinking": 2, "meditating": 2.5,
        "praying": 2.5, "hug": 2.5,
        "kissing": 3, "attacking": 3
    }

    # ---------------- ENV MAP (+20 expanded) ----------------
    env_map = {
        "rose": 3, "flower": 3, "garden": 2.5,
        "rain": 3, "snow": 3, "fog": 2.5,
        "city": 3, "street": 2.5, "forest": 3,
        "mountain": 3, "river": 2.5, "lake": 2.5,
        "sea": 3, "ocean": 3,
        "space": 3, "galaxy": 3, "stars": 3,
        "castle": 3, "room": 2, "indoor": 2,

        # +20 NEW
        "desert": 3, "beach": 3,
        "village": 2.5, "temple": 2.5,
        "church": 2.5, "school": 2,
        "hospital": 2, "office": 2,
        "jungle": 3, "waterfall": 3,
        "cave": 3, "volcano": 3,
        "island": 2.5, "bridge": 2.5,
        "train": 2, "airport": 2,
        "sunrise": 2.5, "sunset": 2.5,
        "storm": 3, "wind": 2.5
    }

    # ---------------- STYLE MAP (+20 expanded) ----------------
    style_map = {
        "sketch": 3, "pencil": 3,
        "watercolor": 3, "oil": 3,
        "cinematic": 2.5,
        "realistic": 2.5,
        "anime": 2.5,
        "fantasy": 3,
        "3d": 3,

        # +20 NEW
        "digital": 2.5,
        "portrait": 2.5,
        "concept": 3,
        "illustration": 2.5,
        "comic": 2.5,
        "manga": 2.5,
        "hdr": 2.5,
        "lowpoly": 3,
        "hyperrealistic": 3,
        "surreal": 3,
        "minimal": 2.5,
        "abstract": 3,
        "graffiti": 2.5,
        "charcoal": 3,
        "ink": 3,
        "pixel": 2.5,
        "retro": 2.5,
        "vintage": 2.5,
        "dark": 2.5,
        "bright": 2
    }

    # ---------------- SCORING ----------------
    for w in words:
        scores["subject"] += subject_map.get(w, 0)
        scores["action"] += action_map.get(w, 0)
        scores["env"] += env_map.get(w, 0)
        scores["style"] += style_map.get(w, 0)

    # ---------------- STYLE DECISION ----------------
    if scores["style"] >= 6:
        style = "hand-drawn pencil sketch, soft shading, artistic illustration"
    elif scores["style"] >= 4:
        style = "cinematic digital artwork, dramatic lighting, depth of field"
    elif scores["style"] > 0:
        style = "semi-realistic detailed illustration"
    else:
        style = "highly detailed digital art"

    # ---------------- SUBJECT CLEANING ----------------
    subject = text
    for k in subject_map.keys():
        subject = subject.replace(k, "")
    subject = subject.strip()

    # ---------------- ACTION + ENVIRONMENT ----------------
    action = "natural expressive pose"
    environment = "detailed background environment"
    mood = "cinematic emotional atmosphere"

    if scores["action"] > 2:
        action = "dynamic or expressive motion based on context"

    if scores["env"] > 2:
        environment = "rich detailed environment matching the scene"

    # ---------------- FINAL PROMPT ----------------
    prompt = f"""
Subject: {subject}
Action: {action}
Environment: {environment}
Art Style: {style}
Mood: {mood}
Composition: professional cinematic framing, balanced structure
Lighting: soft cinematic lighting with realistic shadows
Focus: sharp subject with depth blur background
Quality: ultra detailed, high resolution artistic rendering
"""

    return " ".join(prompt.split())
@app.route("/generate", methods=["GET","POST"])
def generate():
    if not logged():
        return redirect("/")

    out = ""

    if request.method == "POST":

        # 🔥 APPLY PROMPT GENERATOR
        raw_prompt = request.form.get("prompt", "").strip()

        if not raw_prompt:
            return "<div class='msg error'>Prompt cannot be empty</div>"

        enhanced_prompt = prompt_generator(raw_prompt)
        res = generate_ai(enhanced_prompt)

        if res["type"] == "image":
            out = f"<img src='/file/{session['user']}/{res['filename']}'>"

        elif res["type"] == "duplicate":
            out = "<div class='msg error'>⚠ Similar image exists</div>"

        elif res["type"] == "error":
            out = "<div class='msg error'>⚠ AI generation failed</div>"

    return UI + sidebar() + f"""
    <div class='main'>
        <div class='card'>
            <form method='POST'>
                <input name='prompt' placeholder='Enter prompt'>
                <button>Generate</button>
            </form>
            <br>
            {out}
        </div>
    </div>
    """

# (rest of your routes remain EXACTLY same)
@app.route("/prompt", methods=["GET", "POST"])
def prompt():
    if not logged():
        return redirect("/")

    result = ""

    if request.method == "POST":
        text = request.form.get("text", "").strip()

        if not text:
            result = "<div class='msg error'>Prompt cannot be empty</div>"
        else:
            result = prompt_generator(text)

    return UI + sidebar() + f"""
    <div class='main'>
        <div class='card'>
            <h2>🧠 Prompt Generator</h2>
            <form method='POST'>
                <input name='text' placeholder='Describe your image'>
                <button>Generate Prompt</button>
            </form>
            <br>
            <div class='msg success'>{result}</div>
        </div>
    </div>
    """
@app.route("/")
def index():
    return UI + """
    <div class='main'><div class='card'>
    <h2>Welcome to ArtGuard</h2>
    <a href='/login'><button>Login</button></a><br><br>
    <a href='/register'><button>Register</button></a>
    </div></div>
    """
@app.route("/follow/<user>")
def follow(user):
    if not logged():
        return redirect("/")

    if user == session["user"]:
        return redirect("/profile")

    with get_db() as conn:
        cur = conn.cursor()

        cur.execute("""
        SELECT * FROM follows WHERE follower=? AND following=?
        """, (session["user"], user))

        if not cur.fetchone():
            cur.execute("""
            INSERT INTO follows(follower, following, status)
            VALUES(?,?,?)
            """, (session["user"], user, "accepted"))

    return redirect("/profile")
@app.route("/like/<int:image_id>")
def like(image_id):
    if not logged():
        return redirect("/")

    user = session["user"]

    with get_db() as conn:
        cur = conn.cursor()

        cur.execute("SELECT * FROM likes WHERE image_id=? AND user=?", (image_id, user))
        if cur.fetchone():
            return redirect("/public")

        cur.execute("INSERT INTO likes(image_id, user) VALUES(?,?)", (image_id, user))

    return redirect("/public")
@app.route("/login", methods=["GET","POST"])
def login():
    msg=""
    if request.method=="POST":
        email=request.form["email"]
        pwd=hash_password(request.form["password"])

        with get_db() as conn:
            cur=conn.cursor()
            cur.execute("SELECT * FROM users WHERE email=?", (email,))
            u=cur.fetchone()

        if not u:
            msg="<div class='msg error'>User not found</div>"
        elif u[3]!=pwd:
            msg="<div class='msg error'>Wrong password</div>"
        else:
            session["user"]=email
            return redirect("/home")

    return UI+f"""
    <div class='main'><div class='card'>
    <h2>Login</h2>
    <form method='POST'>
    <input name='email' placeholder='Enter Email' required>
    <input name='password' type='password' placeholder='Enter Password' required>
    <button>Login</button>
    </form>{msg}</div></div>
    """
from flask import url_for
import traceback

@app.route("/register", methods=["GET","POST"])
def register():
    msg = ""

    if request.method == "POST":
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "")

        if not email or not password:
            msg = "<div class='msg error'>Email & Password required</div>"
        else:
            try:
                with get_db() as conn:
                    cur = conn.cursor()

                    # check if exists
                    cur.execute("SELECT 1 FROM users WHERE email=?", (email,))
                    if cur.fetchone():
                        msg = "<div class='msg error'>User already exists</div>"
                    else:
                        cur.execute("""
                        INSERT INTO users(email, phone, password)
                        VALUES(?,?,?)
                        """, (email, phone, hash_password(password)))

                        conn.commit()  # 🔥 IMPORTANT

                        session["user"] = email
                        return redirect(url_for("home"))

            except Exception as e:
                traceback.print_exc()
                msg = f"<div class='msg error'>{str(e)}</div>"

    return UI + f"""
    <div class='main'>
        <div class='card'>
            <h2>Register</h2>
            <form method='POST'>
                <input name='email' placeholder='Email' required>
                <input name='phone' placeholder='Phone'>
                <input name='password' type='password' placeholder='Password' required>
                <button>Register</button>
            </form>
            {msg}
        </div>
    </div>
    """
@app.route("/upload", methods=["GET","POST"])
def upload():
    if not logged():
        return redirect("/")

    msg = ""

    if request.method == "POST":
        file = request.files.get("file")
        visibility = request.form.get("visibility")

        if not file or file.filename == "":
            msg = "<div class='msg error'>No file selected</div>"
        else:
            filename = secure_filename(file.filename)
            path = os.path.join(user_folder(), filename)
            file.save(path)

            h = get_hash(path)

            if is_duplicate(h):
                os.remove(path)
                msg = "<div class='msg error'>⚠ Similar image exists</div>"
            else:
                code = uid()
                add_watermark(path, code)

                with get_db() as conn:
                    cur = conn.cursor()

                    # ✅ FIXED INSERT (5 values = 5 columns)
                    cur.execute("""
                        INSERT INTO images(user, filename, visibility, unique_id, image_hash)
                        VALUES(?,?,?,?,?)
                    """, (session["user"], filename, visibility, code, h))

                msg = "<div class='msg success'>Uploaded successfully</div>"

    return UI + sidebar() + f"""
    <div class='main'>
        <div class='card'>
            <form method='POST' enctype='multipart/form-data'>
                <input type='file' name='file' required>

                <select name='visibility'>
                    <option value='private'>Private</option>
                    <option value='public'>Public</option>
                </select>

                <button>Upload</button>
            </form>
            {msg}
        </div>
    </div>
    """
def can_view_private(owner):
    if owner == session["user"]:
        return True

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
        SELECT * FROM follows 
        WHERE follower=? AND following=? AND status='accepted'
        """, (session["user"], owner))

        return cur.fetchone() is not None
@app.route("/gallery")
def gallery():
    if not logged():
        return redirect("/")

    user = session["user"]

    with get_db() as conn:
        cur = conn.cursor()

        cur.execute("""
        SELECT * FROM images WHERE user=?
        """, (user,))

        data = cur.fetchall()

    imgs = ""

    for i in data:
        owner = i[1]
        visibility = i[3]

        if visibility == "private" and not can_view_private(owner):
            continue

        imgs += f"""
        <div class='card'>
        <img src='/file/{i[1]}/{i[2]}'>
        <a href='/like/{i[0]}'>❤️ Like</a>
        </div>
        """

    return UI + sidebar() + f"<div class='main grid'>{imgs}</div>"

@app.route("/public")
def public():
    if not logged():
        return redirect("/")

    user = session["user"]

    with get_db() as conn:
        cur = conn.cursor()

        # ✅ ONLY PUBLIC IMAGES (STRICT FILTER)
        cur.execute("""
            SELECT * FROM images
            WHERE visibility='public'
        """)
        data = cur.fetchall()

    content = ""

    for img in data:
        img_id = img[0]
        owner = img[1]

        with get_db() as conn:
            cur = conn.cursor()

            # 💬 comments
            cur.execute("SELECT user, comment FROM comments WHERE image_id=?", (img_id,))
            comments = cur.fetchall()

            # ❤️ like count
            cur.execute("SELECT COUNT(*) FROM likes WHERE image_id=?", (img_id,))
            like_count = cur.fetchone()[0]

            # 👍 check if liked
            cur.execute("SELECT 1 FROM likes WHERE image_id=? AND user=?", (img_id, user))
            liked = cur.fetchone()

        cm = "".join([f"<p><b>{c[0]}:</b> {c[1]}</p>" for c in comments])
        like_btn = "❤️ Liked" if liked else "🤍 Like"

        content += f"""
        <div class='card'>

        <p><b>👤 {owner}</b></p>

        <img src='/file/{img[1]}/{img[2]}'>

        <p>❤️ {like_count} Likes</p>

        <a href='/like/{img_id}'><button>{like_btn}</button></a>

        <form method='POST' action='/comment/{img_id}'>
        <input name='comment' placeholder='Write comment...'>
        <button>Post</button>
        </form>

        {cm}

        </div>
        """

    return UI + sidebar() + f"<div class='main grid'>{content}</div>"
@app.route("/comment/<int:image_id>", methods=["POST"])
def comment(image_id):
    if not logged(): return redirect("/")
    with get_db() as conn:
        cur=conn.cursor()
        cur.execute("INSERT INTO comments VALUES(NULL,?,?,?)",
                    (image_id,session["user"],request.form["comment"]))
    return redirect("/public")

@app.route("/file/<user>/<filename>")
def file(user, filename):
    return send_from_directory(os.path.join(UPLOAD_FOLDER, user), filename)
@app.route("/users")
def users():
    if not logged():
        return redirect("/")

    current = session["user"]

    with get_db() as conn:
        cur = conn.cursor()

        # get all users except current
        cur.execute("SELECT email FROM users WHERE email!=?", (current,))
        all_users = cur.fetchall()

        html = ""

        for u in all_users:
            uname = u[0]

            # 📌 posts count
            cur.execute("SELECT COUNT(*) FROM images WHERE user=?", (uname,))
            posts = cur.fetchone()[0]

            # 👥 followers count
            cur.execute("""
                SELECT COUNT(*) FROM follows 
                WHERE following=? AND status='accepted'
            """, (uname,))
            followers = cur.fetchone()[0]

            # 🔍 check follow status
            cur.execute("""
                SELECT status FROM follows 
                WHERE follower=? AND following=?
            """, (current, uname))

            status = cur.fetchone()

            # 🎯 BUTTON LOGIC
            if status:
                if status[0] == "pending":
                    btn = "<button disabled>⏳ Requested</button>"

                elif status[0] == "accepted":
                    btn = "<button disabled>✅ Following</button>"
                    btn += f"<a href='/view_profile/{uname}'><button>👁 View Profile</button></a>"

                else:
                    btn = f"<a href='/follow_request/{uname}'><button>➕ Follow</button></a>"

            else:
                btn = f"<a href='/follow_request/{uname}'><button>➕ Follow</button></a>"

            # 🧱 UI CARD
            html += f"""
            <div class='card'>
                <h3>{uname}</h3>
                <p>📌 Posts: {posts}</p>
                <p>👥 Followers: {followers}</p>
                {btn}
            </div>
            """

    return UI + sidebar() + f"<div class='main grid'>{html}</div>"
@app.route("/follow_request/<user>")
def follow_request(user):
    if not logged():
        return redirect("/")

    current = session["user"]

    with get_db() as conn:
        cur = conn.cursor()

        # prevent duplicate request
        cur.execute("""
        SELECT * FROM follows WHERE follower=? AND following=?
        """, (current, user))

        if not cur.fetchone():
            cur.execute("""
            INSERT INTO follows(follower, following, status)
            VALUES(?,?,?)
            """, (current, user, "pending"))

    return redirect("/users")
@app.route("/follow_accept/<int:id>")
def follow_accept(id):
    if not logged():
        return redirect("/")

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE follows SET status='accepted' WHERE id=?", (id,))

    return redirect("/requests")


@app.route("/follow_reject/<int:id>")
def follow_reject(id):
    if not logged():
        return redirect("/")

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM follows WHERE id=?", (id,))

    return redirect("/requests")
@app.route("/requests")
def follow_requests():
    if not logged():
        return redirect("/")

    user = session["user"]

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, follower 
            FROM follows
            WHERE following=? AND status='pending'
            ORDER BY id DESC
        """, (user,))
        reqs = cur.fetchall()

    # ✅ NO REQUESTS UI
    if not reqs:
        html = """
        <div class='card'>
            <h3>📭 No requests yet</h3>
            <p style='opacity:0.7'>When someone sends you a follow request, it will appear here.</p>
        </div>
        """

    # ✅ SHOW REQUESTS
    else:
        html = ""
        for r in reqs:
            html += f"""
            <div class='card'>
                <p><b>👤 {r[1]}</b> wants to follow you</p>

                <div style="display:flex; gap:10px;">
                    <a href='/follow_accept/{r[0]}' style='flex:1;'>
                        <button>✅ Accept</button>
                    </a>

                    <a href='/follow_reject/{r[0]}' style='flex:1;'>
                        <button>❌ Reject</button>
                    </a>
                </div>
            </div>
            """

    return UI + sidebar() + f"<div class='main'>{html}</div>"
@app.route("/profile")
def profile():
    if not logged():
        return redirect("/")

    user = session["user"]

    with get_db() as conn:
        cur = conn.cursor()

        # user details
        cur.execute("SELECT email, phone FROM users WHERE email=?", (user,))
        user_data = cur.fetchone()

        # posts
        cur.execute("SELECT filename, visibility, unique_id FROM images WHERE user=?", (user,))
        posts = cur.fetchall()

        # followers count
        cur.execute("SELECT COUNT(*) FROM follows WHERE following=? AND status='accepted'", (user,))
        followers = cur.fetchone()[0]

        # following count
        cur.execute("SELECT COUNT(*) FROM follows WHERE follower=? AND status='accepted'", (user,))
        following = cur.fetchone()[0]

    post_html = "".join([
        f"<div class='card'><img src='/file/{user}/{p[0]}'><p>🔐 {p[1]}</p><p>🆔 {p[2]}</p></div>"
        for p in posts
    ])

    return UI + sidebar() + f"""
    <div class='main'>
        <div class='card'>
            <h2>👤 Profile</h2>
            <p><b>Email:</b> {user_data[0]}</p>
            <p><b>Phone:</b> {user_data[1]}</p>
            <p>📌 Posts: {len(posts)}</p>
            <p>👥 Followers: {followers}</p>
            <p>➡ Following: {following}</p>
        </div>

        <h3>Your Posts</h3>
        <div class='grid'>
            {post_html}
        </div>
    </div>
    """
@app.route("/home")
def home():
    if not logged():
        return redirect("/")

    user = session["user"]

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM images WHERE user=?", (user,))
        total_posts = cur.fetchone()[0]

    return UI + sidebar() + f"""
    <div class='main'>
        <div class='card'>
            <h2>📊 Dashboard</h2>
            <p>Welcome <b>{user}</b></p>
            <p>🖼 Total Posts: {total_posts}</p>
        </div>
    </div>
    """
@app.route("/view_profile/<user>")
def view_profile(user):
    if not logged():
        return redirect("/")

    current = session["user"]

    # 🔐 ACCESS CONTROL
    allowed = False

    if user == current:
        allowed = True
    else:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT * FROM follows
                WHERE follower=? AND following=? AND status='accepted'
            """, (current, user))

            if cur.fetchone():
                allowed = True

    # ❌ BLOCK ACCESS
    if not allowed:
        return UI + sidebar() + """
        <div class='main'>
            <div class='card'>
                <h3>🔒 Private Account</h3>
                <p>Follow and get accepted to view profile.</p>
            </div>
        </div>
        """

    # ✅ LOAD PROFILE DATA
    with get_db() as conn:
        cur = conn.cursor()

        cur.execute("SELECT email, phone FROM users WHERE email=?", (user,))
        user_data = cur.fetchone()

        cur.execute("SELECT filename, visibility, unique_id FROM images WHERE user=?", (user,))
        posts = cur.fetchall()

        cur.execute("SELECT COUNT(*) FROM follows WHERE following=? AND status='accepted'", (user,))
        followers = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM follows WHERE follower=? AND status='accepted'", (user,))
        following = cur.fetchone()[0]

    # 🔥 IMPORTANT: SHOW ALL POSTS (NO FILTER)
    post_html = "".join([
        f"""
        <div class='card'>
            <img src='/file/{user}/{p[0]}'>
            <p>🔐 {p[1]}</p>
            <p>🆔 {p[2]}</p>
        </div>
        """
        for p in posts
    ])

    return UI + sidebar() + f"""
    <div class='main'>
        <div class='card'>
            <h2>👤 {user}'s Profile</h2>
            <p><b>Email:</b> {user_data[0]}</p>
            <p><b>Phone:</b> {user_data[1]}</p>
            <p>📌 Posts: {len(posts)}</p>
            <p>👥 Followers: {followers}</p>
            <p>➡ Following: {following}</p>
        </div>

        <h3>Posts</h3>
        <div class='grid'>
            {post_html}
        </div>
    </div>
    """
    
@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

# ================= RUN =================
if __name__ == "__main__":
    app.run(debug=True)