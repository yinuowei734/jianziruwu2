"""见字如晤 · 时光邮局

一个复古信件风格的 Streamlit 应用：选信纸、贴邮票、火漆封缄、投递。

运行方式：streamlit run app.py
"""

import base64
import hashlib
import hmac
import html
import io
import os
import re
import secrets
import shutil
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path

import streamlit as st

try:
    from PIL import Image, ImageDraw, ImageOps
except ImportError:  # Pillow 随 Streamlit 一起安装，缺失时退化为原图直出
    Image = ImageDraw = ImageOps = None

# ---------------------------------------------------------------- 页面配置
st.set_page_config(
    page_title="见字如晤·时光邮局",
    page_icon="✉️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------- 素材目录
BASE_DIR = Path(__file__).resolve().parent
ASSET_DIR = BASE_DIR / "assets"
SEAL_PATH = ASSET_DIR / "wax_seal.png"
ENVELOPE_PATH = ASSET_DIR / "envelope.png"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
# 用户随信夹带的照片：单独一个目录，不跟素材混
UPLOAD_DIR = BASE_DIR / "uploads"
PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png"}
# 自制邮票：单独一个目录，每人最多 12 枚
STAMP_DIR = BASE_DIR / "stamps"
STAMP_CANVAS = (460, 640)  # 画布约 5:7，与馆藏邮票的身段一致
STAMP_MARGIN = 30  # 白边宽度
STAMP_PERF_RADIUS = 9  # 齿孔半径
STAMP_PERF_STEP = 32  # 齿孔间距
MAX_MY_STAMPS = 12
# 账户隐私：密码至少 6 位；静置 30 分钟自动上锁
PASSWORD_MIN = 6
PBKDF2_ROUNDS = 120_000
IDLE_LOCK_SECONDS = 30 * 60


def list_prefixed(prefix: str) -> list[str]:
    """取 assets 下按文件名前缀分类的图片：paper_* 是信纸，stamp_* 是邮票。

    不做缓存：往 assets 里丢一张 paper_04.png，刷新页面就能在下拉框里选到。
    """
    if not ASSET_DIR.is_dir():
        return []
    files = [
        p
        for p in ASSET_DIR.iterdir()
        if p.suffix.lower() in IMAGE_SUFFIXES and p.name.lower().startswith(prefix)
    ]
    return sorted(str(p) for p in files)


def _crop_transparent(img, pad: int = 3):
    """按透明通道裁掉四周留白；整张都不透明时退回按亮度找白边。"""
    box = img.split()[-1].getbbox()
    if not box or box == (0, 0, img.width, img.height):
        flat = Image.new("RGB", img.size, (255, 255, 255))
        flat.paste(img, mask=img.split()[-1])
        mask = Image.eval(flat.convert("L"), lambda v: 255 if v < 244 else 0)
        box = mask.getbbox()
    if not box:
        return img
    return img.crop(
        (
            max(box[0] - pad, 0),
            max(box[1] - pad, 0),
            min(box[2] + pad, img.width),
            min(box[3] + pad, img.height),
        )
    )


def encode_image(
    path: str, max_side: int = 1200, trim: bool = False, alpha: bool = False
) -> tuple[str, int, int]:
    """把图片缩放后编码成 base64 Data URI，返回 (uri, 宽, 高)。

    素材原图动辄数百 KB，缩到 max_side 内既能保证清晰度，
    又不至于让每轮 rerun 的页面体积过大。
    trim=True 按透明通道裁掉四周留白；trim/alpha=True 保留透明背景存 PNG，
    否则拍平为 JPEG（信纸这类照片纹理用 JPEG 体积最小）。
    返回的宽高是处理之后的尺寸，据此算排版留白才准确。
    """
    # 同路径的图片被换掉后 mtime/大小会变，缓存随之失效，不会继续用旧图
    stat = Path(path).stat()
    return _encode_cached(str(path), stat.st_mtime_ns, stat.st_size, max_side, trim, alpha)


@st.cache_data(show_spinner=False)
def _encode_cached(
    path: str, mtime_ns: int, size: int, max_side: int, trim: bool, alpha: bool
) -> tuple[str, int, int]:
    """带缓存的编码实现；mtime_ns / size 只参与缓存键，本身不参与计算。"""
    suffix = Path(path).suffix.lower()
    mime = "image/png" if suffix == ".png" else "image/jpeg"
    raw = Path(path).read_bytes()
    width = height = 0
    if Image is not None:
        try:
            with Image.open(io.BytesIO(raw)) as src:
                # 必须先转 RGBA 再处理：直接 convert("RGB") 会把透明底压成黑底
                img = src.convert("RGBA")
                buf = io.BytesIO()
                if trim:
                    img = _crop_transparent(img)
                img.thumbnail((max_side, max_side), Image.LANCZOS)
                if trim or alpha:
                    img.save(buf, format="PNG", optimize=True)
                    mime = "image/png"
                else:
                    img = img.convert("RGB")
                    img.save(buf, format="JPEG", quality=88, optimize=True)
                    mime = "image/jpeg"
                width, height = img.size
                raw = buf.getvalue()
        except OSError:
            pass  # 图片损坏时退回原始字节，不影响页面
    uri = f"data:{mime};base64," + base64.b64encode(raw).decode()
    return uri, width, height


def to_data_uri(path: str, max_side: int = 1200, trim: bool = False, alpha: bool = False) -> str:
    """只取 Data URI。"""
    return encode_image(path, max_side, trim, alpha)[0]


def upload_preview_uri(upload) -> str:
    """把刚上传的图片压成 data URI 预览；图片还在内存里，没落盘。"""
    return _preview_cached(upload.getvalue(), 720)


@st.cache_data(show_spinner=False, max_entries=8)
def _preview_cached(raw: bytes, max_side: int) -> str:
    """预览用缩略图：拍照原图动辄几 MB，不缩会在每次 rerun 时卡住输入。"""
    if Image is not None:
        try:
            with Image.open(io.BytesIO(raw)) as src:
                img = ImageOps.exif_transpose(src).convert("RGB")  # 按 EXIF 摆正
                img.thumbnail((max_side, max_side), Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=86, optimize=True)
                raw = buf.getvalue()
        except OSError:
            pass  # 图片损坏时退回原始字节，不影响页面
    return "data:image/jpeg;base64," + base64.b64encode(raw).decode()


# ---------------------------------------------------------------- 账户
DB_PATH = BASE_DIR / "letter_app.db"
POST_DOMAIN = "shiguang.post"
# 写信页的两种投递方式
MODE_FRIEND = "寄给指定朋友"
MODE_RANDOM = "随缘投递（给陌生人）"
# 前缀：3-20 位，小写字母/数字开头，可含 . _ -
PREFIX_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{2,19}")


@st.cache_resource(show_spinner=False)
def init_db() -> bool:
    """建库建表；cache_resource 让它在整个进程里只跑一次。"""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    try:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    username     TEXT NOT NULL,
                    post_address TEXT NOT NULL UNIQUE,
                    created_at   TEXT NOT NULL,
                    password_hash TEXT,
                    allow_random  INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            # 老库补列：密码与漂流瓶开关都是后加的；老账户首次登录时再设密码
            user_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
            if "password_hash" not in user_columns:
                conn.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
            if "allow_random" not in user_columns:
                conn.execute(
                    "ALTER TABLE users ADD COLUMN allow_random INTEGER NOT NULL DEFAULT 1"
                )
            # 一封信一行；paper / stamp 存文件名，换排版也不失效
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS letters (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender_address   TEXT NOT NULL,
                    receiver_address TEXT NOT NULL,
                    paper            TEXT,
                    stamp            TEXT,
                    content          TEXT NOT NULL,
                    send_time        TEXT NOT NULL,
                    is_read          INTEGER NOT NULL DEFAULT 0,
                    is_random        INTEGER NOT NULL DEFAULT 0,
                    photo_path       TEXT,
                    photo_frame      TEXT
                )
                """
            )
            # 老库补列：漂流瓶与夹带照片都是后加的，已有的信按旧语义补默认值
            columns = {row[1] for row in conn.execute("PRAGMA table_info(letters)")}
            if "is_random" not in columns:
                conn.execute(
                    "ALTER TABLE letters ADD COLUMN is_random INTEGER NOT NULL DEFAULT 0"
                )
            if "photo_path" not in columns:
                conn.execute("ALTER TABLE letters ADD COLUMN photo_path TEXT")
            if "photo_frame" not in columns:
                conn.execute("ALTER TABLE letters ADD COLUMN photo_frame TEXT")
            # 自制邮票：只属于上传的人，写信时出现在他自己的邮票列表里
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS custom_stamps (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_address TEXT NOT NULL,
                    name          TEXT NOT NULL,
                    filename      TEXT NOT NULL UNIQUE,
                    created_at    TEXT NOT NULL
                )
                """
            )
    finally:
        conn.close()
    return True


def _connect() -> sqlite3.Connection:
    """每次操作单开一个连接，用完就关，避免跨线程复用。"""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def find_user(post_address: str) -> dict | None:
    """按完整地址查人，查不到返回 None。password_hash 只用于本地校验，不外显。"""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT username, post_address, created_at, password_hash, allow_random"
            " FROM users WHERE post_address = ?",
            (post_address,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def hash_password(password: str) -> str:
    """密码不存明文：PBKDF2 + 每人一个随机盐，格式 pbkdf2_sha256$轮数$盐$摘要。"""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ROUNDS
    ).hex()
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt}${digest}"


def verify_password(password: str, stored: str | None) -> bool:
    """恒定时间比对摘要，避免把猜测耗时当线索。"""
    if not stored:
        return False
    try:
        algo, rounds, salt, digest = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    calc = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(rounds)
    ).hex()
    return hmac.compare_digest(calc, digest)


def set_password(post_address: str, password: str) -> None:
    """设置或修改密码；老账户首次登录、以及账户面板里改密码都走这里。"""
    conn = _connect()
    try:
        with conn:
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE post_address = ?",
                (hash_password(password), post_address),
            )
    finally:
        conn.close()


def set_allow_random(post_address: str, allow: bool) -> None:
    """隐私开关：是否愿意被漂流瓶随机抽中。"""
    conn = _connect()
    try:
        with conn:
            conn.execute(
                "UPDATE users SET allow_random = ? WHERE post_address = ?",
                (1 if allow else 0, post_address),
            )
    finally:
        conn.close()


def delete_account(post_address: str) -> int:
    """注销账户：连同收发的信件、夹带照片、自制邮票一起清干净，返回删掉几封信。"""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT photo_path FROM letters"
            " WHERE sender_address = ? OR receiver_address = ?",
            (post_address, post_address),
        ).fetchall()
        stamp_rows = conn.execute(
            "SELECT filename FROM custom_stamps WHERE owner_address = ?",
            (post_address,),
        ).fetchall()
        with conn:
            removed = conn.execute(
                "DELETE FROM letters WHERE sender_address = ? OR receiver_address = ?",
                (post_address, post_address),
            ).rowcount
            conn.execute(
                "DELETE FROM custom_stamps WHERE owner_address = ?", (post_address,)
            )
            conn.execute("DELETE FROM users WHERE post_address = ?", (post_address,))
    finally:
        conn.close()
    # 库里的照片文件一并删掉，不留痕迹
    for row in rows:
        rel = row["photo_path"]
        if rel:
            path = BASE_DIR / rel
            if path.is_file() and UPLOAD_DIR in path.parents:
                path.unlink(missing_ok=True)
    # 自制邮票的图片也一并带走
    for row in stamp_rows:
        path = STAMP_DIR / row["filename"]
        if path.is_file():
            path.unlink(missing_ok=True)
    return removed


def random_recipient(exclude_address: str) -> str | None:
    """漂流瓶：从愿意收瓶的人里随机抽一个地址；没有就返回 None。"""
    conn = _connect()
    try:
        row = conn.execute(
            """
            SELECT post_address FROM users
            WHERE post_address != ? AND allow_random = 1
            ORDER BY RANDOM()
            LIMIT 1
            """,
            (exclude_address,),
        ).fetchone()
    finally:
        conn.close()
    return row["post_address"] if row else None


def custom_stamps_for(owner_address: str) -> list[dict]:
    """这个人的集邮册：只返回自己的自制邮票，别人看不到。"""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, name, filename, created_at FROM custom_stamps"
            " WHERE owner_address = ? ORDER BY id DESC",
            (owner_address,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def create_custom_stamp(owner_address: str, name: str, filename: str) -> None:
    """登记一枚自制邮票；filename 是 stamps/ 下的文件名。"""
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _connect()
    try:
        with conn:
            conn.execute(
                "INSERT INTO custom_stamps (owner_address, name, filename, created_at)"
                " VALUES (?, ?, ?, ?)",
                (owner_address, name, filename, created_at),
            )
    finally:
        conn.close()


def stamp_used_in_letters(filename: str) -> bool:
    """已经贴在信上的邮票不能取下，否则别人拆信时会缺一枚票。"""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM letters WHERE stamp = ? LIMIT 1", (filename,)
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def delete_custom_stamp(stamp_id: int, owner_address: str) -> bool:
    """从集邮册取下：只有主人能删；文件一并清掉。"""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT filename FROM custom_stamps WHERE id = ? AND owner_address = ?",
            (stamp_id, owner_address),
        ).fetchone()
        if not row:
            return False
        with conn:
            conn.execute(
                "DELETE FROM custom_stamps WHERE id = ? AND owner_address = ?",
                (stamp_id, owner_address),
            )
    finally:
        conn.close()
    path = STAMP_DIR / row["filename"]
    if path.is_file():
        path.unlink(missing_ok=True)
    return True


def create_user(username: str, post_address: str, password: str) -> dict:
    """写入一条用户记录；地址撞车时抛 sqlite3.IntegrityError。"""
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _connect()
    try:
        with conn:
            conn.execute(
                "INSERT INTO users (username, post_address, created_at, password_hash)"
                " VALUES (?, ?, ?, ?)",
                (username, post_address, created_at, hash_password(password)),
            )
    finally:
        conn.close()
    return {
        "username": username,
        "post_address": post_address,
        "created_at": created_at,
        "allow_random": 1,
    }


def insert_letter(
    sender: str,
    receiver: str,
    paper: str | None,
    stamp: str | None,
    content: str,
    is_random: int = 0,
    photo_path: str | None = None,
    photo_frame: str | None = None,
) -> None:
    """投进信箱：新寄出的信默认未读；photo_path / photo_frame 是照片与相框。"""
    send_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _connect()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO letters
                    (sender_address, receiver_address, paper, stamp,
                     content, send_time, is_random, photo_path, photo_frame)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sender,
                    receiver,
                    paper,
                    stamp,
                    content,
                    send_time,
                    is_random,
                    photo_path,
                    photo_frame,
                ),
            )
    finally:
        conn.close()


def save_photo(upload) -> str | None:
    """把夹带的照片落到 uploads/：uuid 重命名防重名，返回相对路径。"""
    if upload is None:
        return None
    suffix = Path(upload.name).suffix.lower()
    if suffix not in PHOTO_SUFFIXES:
        return None
    os.makedirs(UPLOAD_DIR, exist_ok=True)  # 目录不存在就建
    filename = f"{uuid.uuid4().hex}{suffix}"
    upload.seek(0)
    with open(UPLOAD_DIR / filename, "wb") as target:
        shutil.copyfileobj(upload, target)
    return f"uploads/{filename}"


# ---------------------------------------------------------------- 相框
@st.cache_data(show_spinner=False, max_entries=64)
def _frame_hole_cached(
    frame_path: str, mtime_ns: int, size: int
) -> tuple[int, int, int, int]:
    """在缩小的灰度图上找最大全黑矩形（直方图法），就是相框的洞口。"""
    with Image.open(frame_path) as src:
        gray = src.convert("L")
        wide, tall = gray.size
        scale = min(1.0, 320 / max(wide, tall))
        if scale < 1:
            gray = gray.resize(
                (max(1, int(wide * scale)), max(1, int(tall * scale))), Image.NEAREST
            )
        mask = gray.point(lambda v: 255 if v < 20 else 0)
        w, h = mask.size
        px = mask.load()
        heights = [0] * w
        best_area, bx0, by0, bx1, by1 = 0, 0, 0, 0, 0
        for y in range(h):
            for x in range(w):
                heights[x] = heights[x] + 1 if px[x, y] else 0
            stack: list[tuple[int, int]] = []
            for x in range(w + 1):
                cur = heights[x] if x < w else 0
                start = x
                while stack and stack[-1][1] > cur:
                    sx, sh = stack.pop()
                    area = sh * (x - sx)
                    if area > best_area:
                        best_area = area
                        bx0, by0, bx1, by1 = sx, y - sh + 1, x, y + 1
                    start = sx
                stack.append((start, cur))
        inv = 1 / scale
        return (int(bx0 * inv), int(by0 * inv), int(bx1 * inv), int(by1 * inv))


def frame_hole(frame_path: str) -> tuple[int, int, int, int] | None:
    """洞口位置（左上右下）；没装 Pillow 或文件不在就返回 None。"""
    if Image is None:
        return None
    try:
        stat = Path(frame_path).stat()
    except OSError:
        return None
    return _frame_hole_cached(str(frame_path), stat.st_mtime_ns, stat.st_size)


def _composite(photo, frame_path: str, max_side: int) -> tuple[str, int, int]:
    """把照片裁成洞口的比例嵌进去，返回 (data URI, 宽, 高)。"""
    with Image.open(frame_path) as src:
        frame = src.convert("RGB")
    hole = frame_hole(frame_path)
    if hole:
        x0, y0, x1, y1 = hole
        bw, bh = x1 - x0, y1 - y0
        if bw >= 16 and bh >= 16:
            # 竖拍人像往往脸在上半部分，裁剪重心稍微上移
            fitted = ImageOps.fit(photo, (bw, bh), Image.LANCZOS, centering=(0.5, 0.35))
            frame.paste(fitted, (x0, y0))
    if max(frame.size) > max_side:
        frame.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    frame.save(buf, format="JPEG", quality=88, optimize=True)
    uri = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    return uri, frame.width, frame.height


def compose_photo(photo_path: str, frame_path: str, max_side: int = 1100) -> tuple[str, int, int]:
    """已落盘的照片 + 相框；文件换了 mtime/大小都会让缓存失效。"""
    stat = Path(photo_path).stat()
    fstat = Path(frame_path).stat()
    return _compose_file_cached(
        str(photo_path), stat.st_mtime_ns, stat.st_size,
        str(frame_path), fstat.st_mtime_ns, fstat.st_size, max_side,
    )


@st.cache_data(show_spinner=False, max_entries=32)
def _compose_file_cached(
    photo_path: str, photo_mtime: int, photo_size: int,
    frame_path: str, frame_mtime: int, frame_size: int, max_side: int,
) -> tuple[str, int, int]:
    with Image.open(photo_path) as src:
        photo = ImageOps.exif_transpose(src).convert("RGB")
    return _composite(photo, frame_path, max_side)


def compose_upload(upload, frame_path: str, max_side: int = 720) -> str:
    """写信时的预览：照片还在内存里，直接拿字节合成。"""
    fstat = Path(frame_path).stat()
    return _compose_upload_cached(
        upload.getvalue(), str(frame_path), fstat.st_mtime_ns, fstat.st_size, max_side
    )[0]


@st.cache_data(show_spinner=False, max_entries=16)
def _compose_upload_cached(
    raw: bytes, frame_path: str, mtime_ns: int, size: int, max_side: int
) -> tuple[str, int, int]:
    with Image.open(io.BytesIO(raw)) as src:
        photo = ImageOps.exif_transpose(src).convert("RGB")
    return _composite(photo, frame_path, max_side)


# ---------------------------------------------------------------- 自制邮票
def stamp_path_by_name(name: str | None) -> str | None:
    """自制邮票的文件名 → 绝对路径；不在就返回 None。"""
    if not name:
        return None
    path = STAMP_DIR / Path(name).name
    return str(path) if path.is_file() else None


def _stamp_perforation(size: tuple[int, int]):
    """邮票四周的齿孔：在白底遮罩上沿边打一圈半圆，孔洞透明。"""
    width, height = size
    mask = Image.new("L", size, 255)
    draw = ImageDraw.Draw(mask)
    radius = STAMP_PERF_RADIUS
    cols = max(2, round(width / STAMP_PERF_STEP))
    rows = max(2, round(height / STAMP_PERF_STEP))
    for i in range(cols):
        x = round((i + 0.5) * width / cols)
        draw.ellipse((x - radius, -radius, x + radius, radius), fill=0)
        draw.ellipse(
            (x - radius, height - radius, x + radius, height + radius), fill=0
        )
    for j in range(rows):
        y = round((j + 0.5) * height / rows)
        draw.ellipse((-radius, y - radius, radius, y + radius), fill=0)
        draw.ellipse(
            (width - radius, y - radius, width + radius, y + radius), fill=0
        )
    return mask


def stampify_png(raw: bytes) -> bytes:
    """把任意照片裁成邮票：白边 + 细内框 + 齿孔，返回 PNG 字节。"""
    with Image.open(io.BytesIO(raw)) as src:
        photo = ImageOps.exif_transpose(src).convert("RGB")
    inner = (STAMP_CANVAS[0] - STAMP_MARGIN * 2, STAMP_CANVAS[1] - STAMP_MARGIN * 2)
    fitted = ImageOps.fit(photo, inner, Image.LANCZOS, centering=(0.5, 0.42))
    canvas = Image.new("RGBA", STAMP_CANVAS, (255, 252, 244, 255))
    canvas.paste(fitted, (STAMP_MARGIN, STAMP_MARGIN))
    # 一圈细细的压印线，让照片看起来是被「框」进邮票里的
    ImageDraw.Draw(canvas).rectangle(
        (
            STAMP_MARGIN - 1,
            STAMP_MARGIN - 1,
            STAMP_MARGIN + inner[0],
            STAMP_MARGIN + inner[1],
        ),
        outline=(150, 120, 80, 110),
        width=1,
    )
    canvas.putalpha(_stamp_perforation(STAMP_CANVAS))
    buf = io.BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


@st.cache_data(show_spinner=False, max_entries=8)
def stamp_preview_uri(raw: bytes) -> str:
    """制作邮票时的实时预览。"""
    return "data:image/png;base64," + base64.b64encode(stampify_png(raw)).decode()


def save_custom_stamp(upload, owner_address: str, name: str) -> bool:
    """把上传的照片做成邮票存进 stamps/，并登记到主人的集邮册。"""
    if upload is None or Image is None:
        return False
    try:
        png = stampify_png(upload.getvalue())
    except OSError:
        return False
    os.makedirs(STAMP_DIR, exist_ok=True)
    filename = f"custom_{uuid.uuid4().hex[:12]}.png"
    (STAMP_DIR / filename).write_bytes(png)
    create_custom_stamp(owner_address, name, filename)
    return True


def stamp_choices(address: str) -> list[tuple[str, str]]:
    """写信时的邮票选项：(文件路径, 下拉文字)。馆藏在前，自制在后。"""
    choices = [
        (path, f"邮票 {index:02d}")
        for index, path in enumerate(list_prefixed("stamp_"), 1)
    ]
    taken = {label for _, label in choices}
    for stamp in custom_stamps_for(address):
        path = stamp_path_by_name(stamp["filename"])
        if not path:
            continue
        label = f"我的邮票 · {stamp['name']}"
        base, serial = label, 2
        while label in taken:
            label = f"{base}（{serial}）"
            serial += 1
        taken.add(label)
        choices.append((path, label))
    return choices


def flash_notice() -> None:
    """取一次就消失的提示：寄出后刷新不会重复弹同一个成功框。"""
    message = st.session_state.pop("flash", "")
    if message:
        st.success(message)


def letters_for(address: str) -> list[dict]:
    """收件箱：这个地址收到的全部信，新寄来的排在前面。"""
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT id, sender_address, receiver_address,
                   paper, stamp, content, send_time, is_read, is_random, photo_path,
                   photo_frame
            FROM letters
            WHERE receiver_address = ?
            ORDER BY id DESC
            """,
            (address,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def mark_read(letter_id: int) -> None:
    """拆开即已读。"""
    conn = _connect()
    try:
        with conn:
            conn.execute("UPDATE letters SET is_read = 1 WHERE id = ?", (letter_id,))
    finally:
        conn.close()


def asset_by_name(name: str | None) -> str | None:
    """把库里存的文件名还原成路径：先找 assets，再找自制邮票的 stamps/。"""
    if not name:
        return None
    path = ASSET_DIR / name
    if path.exists():
        return str(path)
    path = STAMP_DIR / name
    return str(path) if path.exists() else None


def normalize_prefix(text: str) -> str:
    """收拾用户输入的前缀：去空格、转小写、去掉误输入的 @。"""
    return text.strip().lower().lstrip("@")


def mask_address(address: str, keep: int = 2) -> str:
    """只露前两位：su***@shiguang.post，共享屏幕时不把完整地址摆出来。"""
    name, sep, domain = address.partition("@")
    masked = f"{name[:keep]}***" if len(name) > keep else f"{name[:1]}***"
    return f"{masked}{sep}{domain}" if sep else masked


def current_user() -> dict | None:
    """当前登录用户；未登录返回 None。"""
    return st.session_state.get("user")


def sign_in(user: dict) -> None:
    """写入登录状态，同时清掉上一封信的痕迹，避免串号。"""
    st.session_state.user = user
    st.session_state.last_active = time.time()
    st.session_state.auth_epoch = st.session_state.get("auth_epoch", 0) + 1
    st.session_state.draft_text = ""
    st.session_state.letter_paper = None
    st.session_state.letter_stamp = None
    st.session_state.stage = "write"
    st.session_state.reading_letter = None
    st.session_state.pop("reply_to", None)
    st.session_state.composer += 1


def sign_out() -> None:
    """退出登录并重置写信状态；换一轮密码框 key，旧密码不留在输入框里。"""
    st.session_state.pop("user", None)
    st.session_state.pop("pending_password", None)
    st.session_state.auth_epoch = st.session_state.get("auth_epoch", 0) + 1
    st.session_state.draft_text = ""
    st.session_state.letter_paper = None
    st.session_state.letter_stamp = None
    st.session_state.stage = "write"
    st.session_state.reading_letter = None
    st.session_state.pop("reply_to", None)
    st.session_state.composer += 1


init_db()


# ---------------------------------------------------------------- 主题
NAV_ITEMS = ("写信", "我的信箱", "邮票陈列馆")
STAMP_WIDTH = 96  # 与 CSS 中 .letter-stamp 的 width 保持一致，供计算排版留白用

MAIL_STEPS = (
    (0.0, "信 已 封 存 ，正 在 分 拣"),
    (0.38, "正 在 加 盖 邮 戳"),
    (0.72, "正 在 送 往 云 端 邮 局"),
)
MAIL_SECONDS = 3.0  # 进度条走完所需的大致秒数

# 写信区容器：靠 .paper-anchor 标记反查父级。
# 用 :is() 把 Streamlit 新旧两套 DOM（stElementContainer / element-container）
# 合并成单个选择器，后续派生规则才能真正拼接在同一个选择器上。
PAPER_CHILD = ':is(div[data-testid="stElementContainer"], div.element-container)'
PAPER_NEW = (
    'div[data-testid="stVerticalBlock"]:has(' f'> {PAPER_CHILD} .paper-anchor)'
)
PAPER_SELECTOR = PAPER_NEW

# 信封阶段（投递 / 送达 / 待拆）的容器：同样靠隐藏标记反查
ENV_SELECTOR = (
    'div[data-testid="stVerticalBlock"]:has(' f'> {PAPER_CHILD} .envelope-anchor)'
)
# 信箱页：靠隐藏标记把整页内容收窄居中
MAILBOX_SELECTOR = (
    'div[data-testid="stVerticalBlock"]:has(' f'> {PAPER_CHILD} .mailbox-anchor)'
)

# ---------------------------------------------------------------- 全局样式
# 颜色 / 字体统一放在 CSS 变量里，样式表本身就不用再做 f-string 转义了
THEME_CSS = """
<style>
:root {
    --font: "Kaiti SC", "STKaiti", "KaiTi", "楷体", "Songti SC", "SimSun", Georgia, serif;

    /* 底色：奶油暖黄，明度抬高、饱和度压低，不再发土发闷 */
    --page-bg: #FBF1DE;
    --page-bg-deep: #F2E2C7;
    --paper: #FFFCF4;

    /* 墨色：不用纯黑，留一点棕调 */
    --ink: #4B3B2B;
    --ink-light: #8C7761;

    /* 火漆：陶土玫瑰，代替刺眼的正红 */
    --seal: #B26B5E;
    --seal-dark: #8E4F44;

    /* 侧边栏：浅木色薄薄一层，不再是深棕木箱 */
    --wood: #F6EAD5;
    --wood-deep: #EBDCC0;
    --wood-line: rgba(150, 118, 78, 0.16);
    --brass: #CBAE7F;
    --brass-light: #EEDDB9;

    /* 投影统一从这里取：全部带大模糊、小不透明度，不再用无模糊硬投影 */
    --shadow-soft: 0 20px 44px -26px rgba(120, 88, 48, 0.55);
    --shadow-raise: 0 10px 22px -14px rgba(120, 88, 48, 0.55);
}
</style>
"""

LAYOUT_CSS = """
<style>
/* ---------- 字体：全站统一楷体 / 衬线 ---------- */
html, body { font-family: var(--font); }
.stApp, .stApp p, .stApp li, .stApp h1, .stApp h2, .stApp h3,
.stApp h4, .stApp h5, .stApp h6, .stApp label, .stApp button,
.stApp input, .stApp textarea, .stApp select,
.stApp [data-testid="stMarkdownContainer"],
.stApp [data-testid="stWidgetLabel"],
.stApp [data-testid="stCaptionContainer"] {
    font-family: var(--font);
}

/* ---------- 页面底色：一层薄薄的奶油暖光 ---------- */
.stApp {
    color: var(--ink);
    background-color: var(--page-bg);
    background-image:
        radial-gradient(120% 70% at 50% -10%, #FFFBF1 0%, rgba(255, 251, 241, 0) 62%),
        radial-gradient(100% 60% at 50% 112%, rgba(206, 172, 122, 0.26) 0%, rgba(255, 255, 255, 0) 66%);
    background-attachment: fixed;
}

/* ---------- 报头 ---------- */
.letter-header { text-align: center; padding: 2.6rem 1rem 0; margin-bottom: 1.9rem; }
.stApp .letter-title {
    font-size: 2.15rem;
    font-weight: 400;
    letter-spacing: 0.36em;
    text-indent: 0.36em;
    color: var(--ink);
    margin: 0;
}
.stApp .letter-subtitle {
    font-size: 0.82rem;
    letter-spacing: 0.3em;
    text-indent: 0.3em;
    color: var(--ink-light);
    margin-top: 0.9rem;
}
.letter-header::after {
    content: "";
    display: block;
    width: 168px;
    height: 1px;
    margin: 1.5rem auto 0;
    background: linear-gradient(90deg, transparent, rgba(180, 150, 110, 0.7), transparent);
}

/* ---------- 侧边栏：一只温润的浅木信匣 ---------- */
[data-testid="stSidebar"] > div:first-child {
    background-color: var(--wood);
    background-image:
        repeating-linear-gradient(90deg, rgba(160, 125, 80, 0.04) 0 1px, transparent 1px 9px),
        linear-gradient(178deg, #FCF4E6 0%, #F6EAD5 52%, #EFDDBE 100%);
    border-right: 1px solid var(--wood-line);
    box-shadow: 16px 0 36px -30px rgba(120, 88, 48, 0.45);
}
[data-testid="stSidebar"] p,
[data-testid="stSidebar"] label {
    color: #7B6450;
}
[data-testid="stSidebar"] [data-testid="stCaptionContainer"] {
    color: rgba(123, 100, 80, 0.72);
}

/* 铭牌：一小块温润的浅铜 */
.toolbox-plate {
    margin: 0 0 1.4rem;
    padding: 0.5rem 0.3rem;
    text-align: center;
    font-size: 0.82rem;
    letter-spacing: 0.3em;
    text-indent: 0.3em;
    color: #7C6142;
    background: linear-gradient(180deg, #FBF0DA 0%, #F0DCB8 100%);
    border: 1px solid rgba(160, 125, 80, 0.26);
    border-radius: 10px;
    box-shadow:
        0 6px 14px -10px rgba(120, 88, 48, 0.5),
        inset 0 1px 0 rgba(255, 255, 255, 0.75);
    text-shadow: 0 1px 0 rgba(255, 255, 255, 0.6);
}

/* 小标题：不抢眼，只做个安静的提示 */
.drawer-label {
    margin: 1.5rem 0 0.6rem;
    font-size: 0.72rem;
    letter-spacing: 0.34em;
    color: rgba(123, 100, 80, 0.62);
}

/* 底部一道细铜线，代替原来的粗拉手 */
.toolbox-handle {
    width: 38%;
    height: 3px;
    margin: 2rem auto 0.6rem;
    border-radius: 2px;
    background: linear-gradient(90deg, transparent, rgba(178, 145, 100, 0.55), transparent);
}

/* 导航 = 一枚枚叠放的纸卡，悬停只轻轻上浮；表单按钮沿用同一套 */
[data-testid="stSidebar"] .stButton > button,
[data-testid="stSidebar"] [data-testid="stFormSubmitButton"] > button {
    width: 100%;
    justify-content: flex-start;
    background: linear-gradient(180deg, #FFFDF7 0%, #FBF1E0 100%);
    border: 1px solid rgba(150, 118, 78, 0.2);
    border-radius: 10px;
    color: #6B5540;
    font-size: 1rem;
    letter-spacing: 0.2em;
    padding: 0.55rem 0.9rem;
    box-shadow: 0 8px 16px -12px rgba(120, 88, 48, 0.55);
    transition: background 0.2s ease, transform 0.16s ease,
        box-shadow 0.2s ease, color 0.2s ease, border-color 0.2s ease;
}
[data-testid="stSidebar"] .stButton > button:hover,
[data-testid="stSidebar"] [data-testid="stFormSubmitButton"] > button:hover {
    background: linear-gradient(180deg, #FFFEFA 0%, #FCF4E6 100%);
    border-color: rgba(178, 107, 94, 0.36);
    color: var(--seal);
    transform: translateY(-1px);
    box-shadow: 0 14px 24px -16px rgba(120, 88, 48, 0.65);
}
[data-testid="stSidebar"] .stButton > button:active,
[data-testid="stSidebar"] .stButton > button:focus:not(:active),
[data-testid="stSidebar"] [data-testid="stFormSubmitButton"] > button:active,
[data-testid="stSidebar"] [data-testid="stFormSubmitButton"] > button:focus:not(:active) {
    background: linear-gradient(180deg, #FAF1E2 0%, #F5E7CE 100%);
    border-color: rgba(178, 107, 94, 0.36);
    color: var(--seal-dark);
    box-shadow: inset 0 2px 6px rgba(150, 118, 78, 0.16);
}

/* 侧边栏下拉框：小小的米白卡片 */
[data-testid="stSidebar"] div[data-baseweb="select"] > div,
[data-testid="stSidebar"] [data-testid="stSelectbox"] div[role="group"] {
    background-color: #FFFDF7;
    border: 1px solid rgba(150, 118, 78, 0.22);
    border-radius: 10px;
    color: #5A4632;
    box-shadow: 0 6px 14px -14px rgba(120, 88, 48, 0.7);
}

/* ---------- 信纸卡片 ---------- */
.paper-sheet {
    max-width: 600px;
    margin: 0 auto 1.6rem;
    background-color: var(--paper);
    border: 1px solid rgba(140, 112, 76, 0.16);
    border-radius: 14px;
    box-shadow: var(--shadow-soft);
    padding: 2.4rem 2.8rem;
}
.paper-sheet h3 {
    letter-spacing: 0.2em;
    font-weight: 400;
    margin-top: 0;
    color: #55432F;
}

/* ---------- 写信区容器：背景图片由所选信纸注入 ---------- */
.paper-anchor { display: none; }

__PAPER__ {
    position: relative;
    max-width: 600px;
    margin: 0 auto;
    min-height: 520px;
    padding: 3.6rem 3.2rem 2.8rem;
    border: 1px solid rgba(140, 112, 76, 0.16);
    border-radius: 14px;
    box-shadow: var(--shadow-soft);
    background-color: var(--paper);
    background-repeat: no-repeat;
    background-position: center center;
    background-size: cover;
    overflow: hidden;
}

/* 主内容区作为尺寸容器：纸面按「纸宽 600 时」的原图高度定最小高度。
   窄屏不套比例，纸就随内容自然变长，正文框与按钮永远不会被裁掉。 */
div[data-testid="stMainBlockContainer"],
.block-container {
    container-type: inline-size;
}

/* 贴在信纸上的邮票：只沾一点点歪 */
.letter-stamp {
    position: absolute;
    top: 1.5rem;
    right: 1.5rem;
    width: 96px;
    height: auto;
    transform: rotate(1.6deg);
    filter: drop-shadow(0 3px 6px rgba(110, 84, 50, 0.14));
    pointer-events: none;
    z-index: 6;
}

/* 邮票所在的元素默认是 relative，会抢走定位基准，退回 static 才能
   让绝对定位的邮票以信纸容器为参照 */
__PAPER__ > __CHILD__:has(.letter-stamp) {
    position: static !important;
}

/* ---------- 输入框 ---------- */
.stTextArea textarea {
    background-color: var(--paper);
    border: 1px solid rgba(140, 112, 76, 0.2);
    border-radius: 10px;
    font-family: var(--font);
    font-size: 1.02rem;
    line-height: 2.2;
    color: var(--ink);
    padding: 1.1rem 1.3rem;
    resize: none;
}
.stTextArea textarea::placeholder { color: rgba(140, 119, 97, 0.55); font-style: italic; }
.stTextArea textarea:focus {
    border-color: rgba(178, 107, 94, 0.5);
    box-shadow: 0 0 0 3px rgba(178, 107, 94, 0.1);
}

/* 信纸上的输入框：透明化，与纸面融为一体 */
__PAPER__ .stTextArea textarea {
    background-color: transparent !important;
    border: none !important;
    box-shadow: none !important;
    outline: none;
    padding: 0.2rem 0.1rem;
}
__PAPER__ .stTextArea div[data-baseweb="textarea"],
__PAPER__ .stTextArea > div {
    background-color: transparent !important;
    border: none !important;
    box-shadow: none !important;
}

/* ---------- 信纸上的表单：收件人与纸票 ---------- */
/* 表单比读信高，顶部留白收一点；高度由内容或比例里的较大值决定 */
__PAPER__:has([data-testid="stTextInput"]) {
    padding: 2.4rem 2.6rem 2.2rem;
}
__PAPER__ [data-testid="stTextInput"] input {
    background-color: transparent;
    color: var(--ink);
    font-family: var(--font);
    font-size: 1rem;
    letter-spacing: 0.04em;
}
__PAPER__ [data-testid="stTextInput"] > div > div,
__PAPER__ [data-testid="stSelectbox"] > div > div {
    background-color: rgba(255, 252, 244, 0.6);
    border: 1px solid rgba(150, 118, 78, 0.22);
    border-radius: 10px;
}
__PAPER__ [data-testid="stSelectbox"] [data-baseweb="select"] * {
    font-family: var(--font);
    color: #6B5540;
}
__PAPER__ [data-testid="stWidgetLabel"] p {
    font-size: 0.78rem;
    letter-spacing: 0.16em;
    color: rgba(123, 100, 80, 0.9);
}
/* 选中的邮票缩成一小块，斜斜地贴在纸边 */
.stamp-pick {
    display: block;
    width: 62px;
    margin: 0.45rem 0 0 0.2rem;
    transform: rotate(-2deg);
    filter: drop-shadow(0 4px 8px rgba(110, 84, 50, 0.18));
}
/* 漂流瓶：随缘投递时替掉收件人输入框的一句提示 */
.stApp .drift-tip {
    margin: 0.15rem 0 0.7rem;
    padding: 0.72rem 1rem;
    text-align: center;
    font-size: 0.86rem;
    letter-spacing: 0.08em;
    color: #6E5738;
    background: rgba(255, 252, 244, 0.58);
    border: 1px dashed rgba(150, 118, 78, 0.4);
    border-radius: 10px;
}
/* 回信：收件人下面标一句「正在回复谁」 */
.stApp .reply-hint {
    margin: -0.35rem 0 0.6rem;
    padding-left: 0.6rem;
    border-left: 2px solid rgba(178, 145, 100, 0.55);
    font-size: 0.74rem;
    letter-spacing: 0.08em;
    color: #8A6A44;
}
/* 随信夹带的照片：10px 白边 + 下方留白 + 投影，像一张拍立得 */
.stApp .polaroid {
    display: block;
    width: min(76%, 360px);
    margin: 1.5rem auto 1.3rem;
    padding: 10px 10px 36px;
    background: #ffffff;
    border-radius: 2px;
    box-shadow: 0 12px 26px rgba(86, 62, 38, 0.26), 0 3px 8px rgba(86, 62, 38, 0.14);
    transform: rotate(-1.4deg);
}
.stApp .polaroid img {
    display: block;
    width: 100%;
    height: auto;
}
/* 写信时的预览：缩成一小张，别把信纸撑太长 */
.stApp .polaroid.small {
    width: min(46%, 190px);
    margin: 0.9rem auto 0.9rem;
    padding: 7px 7px 22px;
    transform: rotate(-2deg);
}
/* 装了相框的照片：相框自带卡纸，不再额外加白边 */
.stApp .photo-frame {
    display: block;
    width: min(78%, 360px);
    margin: 1.5rem auto 1.2rem;
    border-radius: 3px;
    box-shadow: 0 14px 30px rgba(86, 62, 38, 0.3), 0 3px 10px rgba(86, 62, 38, 0.16);
    transform: rotate(-1.2deg);
    overflow: hidden;
}
.stApp .photo-frame img {
    display: block;
    width: 100%;
    height: auto;
}
.stApp .photo-frame.small {
    width: min(56%, 210px);
    margin: 0.9rem auto 0.9rem;
    transform: rotate(-2deg);
}

/* ---------- 邮票陈列馆 ---------- */
.stApp .gallery-head {
    max-width: 960px;
    margin: 0 auto 0.6rem;
    padding: 0.6rem 1rem 0;
    text-align: center;
}
.stApp .gallery-head h3 {
    margin: 0;
    font-weight: 400;
    font-size: 1.5rem;
    letter-spacing: 0.3em;
    text-indent: 0.3em;
    color: var(--ink);
}
.stApp .gallery-head p {
    margin: 0.7rem 0 0;
    font-size: 0.82rem;
    letter-spacing: 0.16em;
    color: var(--ink-light);
}
/* 邮票柜：一枚一张小卡，缩略图微微歪着 */
.stApp .stamp-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(104px, 1fr));
    gap: 1rem;
    max-width: 960px;
    margin: 1.2rem auto 0.4rem;
}
.stApp .stamp-case {
    margin: 0;
    padding: 0.9rem 0.5rem 0.7rem;
    text-align: center;
    background: rgba(255, 252, 244, 0.66);
    border: 1px solid rgba(150, 118, 78, 0.18);
    border-radius: 12px;
    box-shadow: var(--shadow-raise);
}
.stamp-case img {
    width: 76px;
    height: auto;
    filter: drop-shadow(0 5px 9px rgba(110, 84, 50, 0.2));
    transform: rotate(-1.5deg);
}
.stamp-case figcaption {
    margin-top: 0.5rem;
    font-size: 0.68rem;
    letter-spacing: 0.14em;
    color: #8A6A44;
}
.stamp-case figcaption span {
    display: block;
    margin-top: 0.2rem;
    color: rgba(140, 119, 97, 0.8);
}
/* 自制邮票的预览：单枚立起来 */
.stApp .stamp-preview { margin: 0.8rem auto 0; text-align: center; }
.stApp .stamp-preview img {
    width: 108px;
    height: auto;
    filter: drop-shadow(0 8px 14px rgba(110, 84, 50, 0.24));
    transform: rotate(-2deg);
}
.stApp .stamp-preview.large img { width: 148px; }
.stApp .gallery-label {
    margin: 1.8rem 0 0.6rem;
    padding-top: 1.2rem;
    border-top: 1px dashed rgba(150, 118, 78, 0.28);
    font-size: 0.72rem;
    letter-spacing: 0.34em;
    color: rgba(123, 100, 80, 0.62);
}
/* 陈列馆里的标签页：安静一点 */
.stApp [data-testid="stTabs"] [data-baseweb="tab-list"] {
    justify-content: center;
    gap: 1.6rem;
    background: transparent;
}
.stApp [data-testid="stTabs"] button[data-baseweb="tab"] {
    font-family: var(--font);
    letter-spacing: 0.2em;
    color: #8A6A44;
}
.stApp [data-testid="stTabs"] button[aria-selected="true"] { color: var(--seal); }
.stApp [data-testid="stTabs"] [data-baseweb="tab-highlight"] {
    background-color: rgba(178, 107, 94, 0.6);
}

/* ---------- 按钮 ---------- */
.stButton > button {
    font-family: var(--font);
    border-radius: 10px;
    border: 1px solid rgba(150, 118, 78, 0.28);
    background: linear-gradient(180deg, #FFFDF6 0%, #F7EAD4 100%);
    color: #6B5540;
    letter-spacing: 0.26em;
    text-indent: 0.26em;
    padding: 0.5rem 1.3rem;
    box-shadow: var(--shadow-raise);
    transition: background 0.2s ease, box-shadow 0.2s ease,
        color 0.2s ease, border-color 0.2s ease;
}
.stButton > button:hover {
    border-color: rgba(178, 107, 94, 0.42);
    color: var(--seal);
    background: linear-gradient(180deg, #FFFCF3 0%, #F9EEDB 100%);
    box-shadow: 0 14px 26px -16px rgba(120, 88, 48, 0.65);
}

/* 纸尾的操作按钮：延伸到纸面下方，且水平居中
   父级竖向容器是 align-items:start，子容器会被收缩成按钮宽度，
   此时 justify-content:center 无从生效，必须先把它撑满一行 */
__PAPER__ > __CHILD__:has(.stButton) { margin-top: auto; align-self: stretch; width: 100%; }
__PAPER__ .stButton { display: flex; justify-content: center; width: 100%; }

/* 回执内容垂直居中 */
__PAPER__ > __CHILD__:has(.letter-read) { margin: auto 0; }

/* ---------- 读信：纸上的正文 ---------- */
.letter-read {
    max-height: 430px;
    overflow-y: auto;
    white-space: pre-wrap;
    line-height: 2.2;
    font-size: 1.02rem;
    color: var(--ink);
}
.stApp .letter-sign {
    margin: 1.9rem 0 1.1rem;
    text-align: right;
    font-size: 0.8rem;
    letter-spacing: 0.18em;
    color: rgba(140, 119, 97, 0.9);
}

/* ---------- 信封：封存 / 投递 / 待拆共用 ---------- */
.wax-seal { width: 158px; height: auto; }

.envelope-stage { text-align: center; padding: 0.4rem 0 0; }
.envelope-wrap { position: relative; display: inline-block; line-height: 0; }
.envelope-wrap.waiting { animation: envelope-breathe 3.6s ease-in-out infinite; }
@keyframes envelope-breathe {
    0%, 100% { transform: translateY(0) rotate(-0.5deg); }
    50% { transform: translateY(-7px) rotate(0.5deg); }
}
.envelope-img {
    width: min(420px, 72vw);
    height: auto;
    filter: drop-shadow(0 26px 32px rgba(120, 88, 48, 0.26));
}
/* 火漆压在信封背面的 V 形封口尖上 */
.envelope-seal {
    position: absolute;
    left: 49.3%;
    top: 58%;
    width: 74px;
    transform: translate(-50%, -50%) rotate(-5deg);
    filter: drop-shadow(0 6px 10px rgba(120, 70, 50, 0.3));
}
.envelope-seal .wax-seal { width: 100%; }

.stApp .env-hint {
    margin: 1.6rem 0 0;
    font-size: 0.94rem;
    letter-spacing: 0.28em;
    text-indent: 0.28em;
    color: #8A7460;
}
.stApp .deliver-text {
    margin: 0.3rem 0 0;
    text-align: center;
    font-size: 1.16rem;
    letter-spacing: 0.22em;
    text-indent: 0.22em;
    color: #55432F;
}

/* 信封阶段的进度条与按钮收窄居中 */
__ENV__ div[data-testid="stProgress"] { max-width: 380px; margin: 1.2rem auto 0; }
__ENV__ > __CHILD__:has(.stButton) { align-self: stretch; width: 100%; }
__ENV__ .stButton { display: flex; justify-content: center; width: 100%; }
__ENV__ div[data-testid="stHorizontalBlock"] { max-width: 460px; margin: 1.4rem auto 0; }
__ENV__ .stButton > button {
    letter-spacing: 0.14em;
    text-indent: 0.14em;
    font-size: 0.94rem;
    padding: 0.5rem 0.8rem;
    white-space: nowrap;
}
__ENV__ .stButton > button[kind="primary"] {
    background: linear-gradient(180deg, #C68274 0%, #AC6153 100%);
    border-color: rgba(140, 76, 64, 0.45);
    color: #FFF8EC;
    box-shadow: 0 12px 22px -12px rgba(140, 76, 64, 0.7);
}
__ENV__ .stButton > button[kind="primary"]:hover {
    background: linear-gradient(180deg, #CE9082 0%, #B76C5E 100%);
    color: #FFFBF4;
}

/* ---------- 侧边栏素材预览 ---------- */
.asset-preview {
    width: 100%;
    margin-top: 0.5rem;
    border: 1px solid var(--wood-line);
    border-radius: 8px;
    box-shadow: 0 10px 20px -16px rgba(120, 88, 48, 0.7);
    background-color: var(--paper);
}

/* ---------- 侧边栏：账户与表单 ---------- */
[data-testid="stSidebar"] [data-testid="stForm"] {
    border: none;
    padding: 0;
    background: transparent;
}
[data-testid="stSidebar"] [data-testid="stTextInput"] input {
    background-color: #FFFDF7;
    color: #4B3B2B;
    font-size: 0.9rem;
    letter-spacing: 0.04em;
}
[data-testid="stSidebar"] [data-testid="stTextInput"] > div > div {
    background-color: #FFFDF7;
    border: 1px solid rgba(150, 118, 78, 0.22);
    border-radius: 10px;
}
[data-testid="stSidebar"] [data-testid="stWidgetLabel"] p {
    font-size: 0.78rem;
    letter-spacing: 0.16em;
    color: rgba(123, 100, 80, 0.85);
}
/* 表单里那句英文「Press Enter to submit form」会压住长地址，隐去 */
[data-testid="stSidebar"] [data-testid="InputInstructions"] {
    display: none;
}

.account-card {
    margin: 0 0 0.7rem;
    padding: 0.7rem 0.9rem;
    background: linear-gradient(180deg, #FFFDF7 0%, #FBF1E0 100%);
    border: 1px solid rgba(150, 118, 78, 0.2);
    border-radius: 10px;
    box-shadow: 0 8px 16px -12px rgba(120, 88, 48, 0.55);
}
/* 隐私与安全展开区里的小标题 */
.stApp .side-note {
    margin: 0.9rem 0 0.35rem;
    font-size: 0.72rem;
    letter-spacing: 0.16em;
    color: #8A6A44;
}
.stApp .account-name {
    margin: 0;
    font-size: 0.95rem;
    letter-spacing: 0.12em;
    color: #55432F;
}
.stApp .account-addr {
    margin: 0.25rem 0 0;
    font-size: 0.74rem;
    letter-spacing: 0.04em;
    color: var(--seal);
    word-break: break-all;
}

/* ---------- 主区：未登录的提示卡 ---------- */
.gate-card {
    max-width: 520px;
    margin: 1.6rem auto 0;
    padding: 2.8rem 2.4rem 3rem;
    text-align: center;
    background-color: var(--paper);
    border: 1px solid rgba(140, 112, 76, 0.16);
    border-radius: 14px;
    box-shadow: var(--shadow-soft);
}
.gate-seal { display: flex; justify-content: center; }
.gate-seal .wax-seal { width: 86px; opacity: 0.9; }
.stApp .gate-text {
    margin: 1.5rem 0 0;
    font-size: 1.02rem;
    letter-spacing: 0.1em;
    color: #55432F;
}
.stApp .gate-sub {
    margin: 0.8rem 0 0;
    font-size: 0.8rem;
    letter-spacing: 0.06em;
    color: rgba(140, 119, 97, 0.9);
}

/* ---------- 主区：登录后的欢迎条 ---------- */
.welcome-bar {
    max-width: 600px;
    margin: 0 auto 1.2rem;
    padding: 0.6rem 1.1rem;
    text-align: center;
    background: rgba(255, 252, 244, 0.7);
    border: 1px solid rgba(140, 112, 76, 0.14);
    border-radius: 10px;
}
.stApp .welcome-text {
    margin: 0;
    font-size: 0.86rem;
    letter-spacing: 0.08em;
    color: #7B6450;
}
.welcome-addr {
    color: var(--seal);
    letter-spacing: 0.1em;
}

/* ---------- 我的信箱：未读卡片与已读列表 ---------- */
.mailbox-anchor { display: none; }

__MAILBOX__ {
    max-width: 660px;
    margin: 0 auto;
}
.stApp .mail-section {
    margin: 1.4rem 0 0.8rem;
    font-size: 0.8rem;
    letter-spacing: 0.28em;
    color: rgba(123, 100, 80, 0.9);
}
.mail-env {
    display: block;
    width: 100%;
    max-width: 106px;
    height: auto;
    filter: drop-shadow(0 10px 16px rgba(120, 88, 48, 0.22));
    transform: rotate(-1.5deg);
}
.stApp .mail-from {
    margin: 0;
    font-size: 0.98rem;
    letter-spacing: 0.05em;
    color: #4B3B2B;
    word-break: break-all;
}
/* 漂流瓶来信：在「来自谁」后面贴一枚小签 */
.stApp .drift-badge {
    display: inline-block;
    margin-left: 0.5rem;
    padding: 0.1rem 0.5rem;
    font-size: 0.68rem;
    letter-spacing: 0.14em;
    color: #8A5A32;
    background: rgba(196, 160, 110, 0.24);
    border-radius: 999px;
    vertical-align: 0.12em;
    white-space: nowrap;
}
.stApp .mail-when {
    margin: 0.3rem 0 0.55rem;
    font-size: 0.76rem;
    letter-spacing: 0.04em;
    color: rgba(140, 119, 97, 0.9);
}
.stApp .mail-empty {
    margin: 0.2rem 0 1.4rem;
    font-size: 0.86rem;
    letter-spacing: 0.08em;
    color: rgba(140, 119, 97, 0.85);
}
.mail-rule {
    height: 1px;
    margin: 0.9rem 0;
    background: linear-gradient(90deg, transparent, rgba(178, 145, 100, 0.4), transparent);
}

/* ---------- 页脚 ---------- */
.letter-footer {
    text-align: center;
    font-size: 0.76rem;
    letter-spacing: 0.22em;
    color: rgba(140, 119, 97, 0.9);
    margin-top: 3.4rem;
    padding: 1rem 0 2.4rem;
    position: relative;
}
.letter-footer::before {
    content: "";
    display: block;
    width: 140px;
    height: 1px;
    margin: 0 auto 1.1rem;
    background: linear-gradient(90deg, transparent, rgba(180, 150, 110, 0.65), transparent);
}
</style>
"""

CSS = (
    THEME_CSS
    + LAYOUT_CSS.replace("__CHILD__", PAPER_CHILD)
    .replace("__ENV__", ENV_SELECTOR)
    .replace("__PAPER__", PAPER_SELECTOR)
    .replace("__MAILBOX__", MAILBOX_SELECTOR)
)
st.markdown(CSS, unsafe_allow_html=True)


# ---------------------------------------------------------------- 小组件
def seal_img(extra_class: str = "") -> str:
    """火漆印的 <img>，素材缺失时返回空串。"""
    if not SEAL_PATH.exists():
        return ""
    uri = to_data_uri(str(SEAL_PATH), 360, alpha=True)
    cls = f"wax-seal {extra_class}".strip()
    return f'<img class="{cls}" src="{uri}" alt="火漆印">'


def render_header() -> None:
    """页面顶部的报头。"""
    st.markdown(
        """
        <div class="letter-header">
            <p class="letter-title">见字如晤</p>
            <p class="letter-subtitle">时 光 邮 局 · 一 纸 山 河</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------- 信件纸面
def render_write() -> None:
    """写信页 / 读信页共用一张信纸：铺纸、贴票，再往纸上放内容。"""
    if st.session_state.stage == "reading":
        render_read_sheet()
    else:
        render_write_sheet()


def render_read_sheet() -> None:
    """读信：按寄出那一刻的纸与票快照还原。"""
    paper_path = st.session_state.get("letter_paper")
    stamp_path = st.session_state.get("letter_stamp")

    rules: list[str] = []
    floor = ""
    if paper_path:
        paper_uri, paper_w, paper_h = encode_image(paper_path, 1400)
        rules.append(f'background-image: url("{paper_uri}");')
        if paper_w and paper_h:
            floor = (
                f"@container (min-width: 600px) {{ {PAPER_SELECTOR} "
                f"{{ min-height: calc(600px * {paper_h} / {paper_w}); }} }}"
            )

    # 邮票按裁剪后的实际高度下压书写区，正文才不会压到邮票上
    stamp_html = ""
    if stamp_path:
        stamp_uri, stamp_w, stamp_h = encode_image(stamp_path, 420, True)
        stamp_box = STAMP_WIDTH * stamp_h / stamp_w if stamp_w else STAMP_WIDTH
        rules.append(f"padding-top: {25.6 + stamp_box + 18:.0f}px;")
        stamp_html = f'<img class="letter-stamp" src="{stamp_uri}" alt="邮票">'

    if rules or floor:
        st.markdown(
            f"<style>{PAPER_SELECTOR} {{ {' '.join(rules)} }} {floor}</style>",
            unsafe_allow_html=True,
        )

    with st.container():
        st.markdown(f'<div class="paper-anchor"></div>{stamp_html}', unsafe_allow_html=True)
        render_letter()


def render_write_sheet() -> None:
    """写信：纸面跟着下拉框里选的那张信纸走。"""
    papers = list_prefixed("paper_")
    paper_labels = [f"信纸 {i:02d}" for i in range(1, len(papers) + 1)]

    # 首帧控件还没渲染，按第一项铺；之后读的就是用户上一次的选择
    default_label = paper_labels[0] if paper_labels else ""
    chosen = st.session_state.get(f"paper_{st.session_state.composer}", default_label)
    index = paper_labels.index(chosen) if chosen in paper_labels else 0
    paper_path = papers[index] if papers else None

    if paper_path:
        paper_uri, paper_w, paper_h = encode_image(paper_path, 1400)
        # 纸宽到 600 时按原图比例定一个最小高度，背景才不会被拉变形；
        # 用 min-height 而不是 aspect-ratio：内容多了纸会自己变长，不会压扁正文框。
        floor = ""
        if paper_w and paper_h:
            floor = (
                f"@container (min-width: 600px) {{ {PAPER_SELECTOR} "
                f"{{ min-height: calc(600px * {paper_h} / {paper_w}); }} }}"
            )
        st.markdown(
            f'<style>{PAPER_SELECTOR} {{ background-image: url("{paper_uri}"); }}'
            f"{floor}</style>",
            unsafe_allow_html=True,
        )

    with st.container():
        # 标记元素：CSS 靠 .paper-anchor 定位到本容器，再叠加信纸与邮票
        st.markdown('<div class="paper-anchor"></div>', unsafe_allow_html=True)
        render_compose(papers, paper_labels)


REPLY_QUOTE_PREFIX = "—— 回复 "


def reply_quote(address: str) -> str:
    """回信时预填在正文里的一行引用。"""
    return f"{REPLY_QUOTE_PREFIX}{address} 的来信"


def strip_reply_quote(content: str) -> str:
    """去掉预填的引用行，用来判断正文是不是真写了东西。"""
    return "\n".join(
        line
        for line in content.splitlines()
        if not line.strip().startswith(REPLY_QUOTE_PREFIX)
    ).strip()


def render_compose(papers: list[str], paper_labels: list[str]) -> None:
    """写信表单：寄给谁、用什么纸、贴什么票、写什么。"""
    flash_notice()
    epoch = st.session_state.composer
    # 馆藏邮票 + 自己做的邮票，按同一份列表铺开
    stamp_options = stamp_choices((current_user() or {}).get("post_address", ""))
    reply_to = st.session_state.get("reply_to")  # 回信时带过来的原寄件人

    # 最上方先定投递方式：指名道姓，还是交给时光邮差
    mode = st.radio(
        "投递方式",
        [MODE_FRIEND, MODE_RANDOM],
        horizontal=True,
        label_visibility="collapsed",
        key=f"mode_{epoch}",
    )
    if mode == MODE_RANDOM:
        recipient = ""  # 随机投递不收地址
        st.markdown(
            '<p class="drift-tip">您的信件将由时光邮差随机送达一位有缘人</p>',
            unsafe_allow_html=True,
        )
    else:
        recipient = st.text_input(
            "收件人地址",
            value=reply_to or "",  # 回信：默认就是原寄件人，用户可改可清
            placeholder="请输入对方的专属邮局地址",
            key=f"to_{epoch}",
        )
        if reply_to:
            st.markdown(
                f'<p class="reply-hint">正在回复 {html.escape(reply_to)} 的来信</p>',
                unsafe_allow_html=True,
            )

    left, right = st.columns(2)
    with left:
        if paper_labels:
            paper_label = st.selectbox("选择信纸", paper_labels, key=f"paper_{epoch}")
            paper_file = Path(papers[paper_labels.index(paper_label)]).name
        else:
            paper_label, paper_file = "", None
            st.caption("assets/ 下还没有 paper_*.png")
    with right:
        stamp_labels = ["不贴邮票"] + [label for _, label in stamp_options]
        stamp_label = st.selectbox("选择邮票", stamp_labels, key=f"stamp_{epoch}")
        stamp_index = stamp_labels.index(stamp_label) - 1
        stamp_path = stamp_options[stamp_index][0] if stamp_index >= 0 else None
        stamp_file = Path(stamp_path).name if stamp_path else None
        if stamp_path:
            preview = to_data_uri(stamp_path, 200, True)
            st.markdown(
                f'<img class="stamp-pick" src="{preview}" alt="邮票">',
                unsafe_allow_html=True,
            )

    content = st.text_area(
        "信件正文",
        value=f"\n\n{reply_quote(reply_to)}" if reply_to else "",
        height=300,
        placeholder="提笔写下你的心意…",
        key=f"body_{epoch}",
    )

    # 正文之下、寄出之前：夹带一张照片
    photo = st.file_uploader(
        "■夹带一张照片(支持jpg/png,从相册或电脑选择)",
        type=["jpg", "jpeg", "png"],
        accept_multiple_files=False,
        key=f"photo_{epoch}",
    )
    frame_file = None
    if photo is not None:
        frame_names = list_prefixed("frame_")
        frame_labels = ["不装相框"] + [
            f"相框 {i:02d}" for i in range(1, len(frame_names) + 1)
        ]
        frame_label = st.selectbox(
            "为照片挑一只相框",
            frame_labels,
            index=1 if len(frame_labels) > 1 else 0,  # 默认就用第一只相框
            key=f"frame_{epoch}",
        )
        frame_index = frame_labels.index(frame_label) - 1
        if frame_index >= 0:
            frame_file = Path(frame_names[frame_index]).name
            st.markdown(
                f'<div class="photo-frame small"><img '
                f'src="{compose_upload(photo, str(ASSET_DIR / frame_file))}"'
                f' alt="照片预览"></div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                f'<div class="polaroid small"><img src="{upload_preview_uri(photo)}"'
                f' alt="照片预览"></div>',
                unsafe_allow_html=True,
            )

    if st.button("封 存 并 寄 出"):
        send_letter(
            mode, recipient, paper_file, stamp_file, content, photo, frame_file
        )


def send_letter(
    mode: str,
    recipient: str,
    paper_file: str | None,
    stamp_file: str | None,
    content: str,
    photo=None,
    photo_frame: str | None = None,
) -> None:
    """封缄寄出：指定投递先查人，随缘投递交给时光邮差抽人，最后都入库。"""
    sender = (current_user() or {}).get("post_address", "")

    if not strip_reply_quote(content):
        st.warning("信纸尚空，先落几笔吧。")
        return

    if mode == MODE_RANDOM:
        address = random_recipient(sender)  # SQL 里 ORDER BY RANDOM() 抽一位
        if not address:
            st.warning("时光邮局暂无其他有缘人，请稍后再试或邀请朋友注册")
            return
        is_random = 1
    else:
        address = recipient.strip().lower()
        if not address:
            st.error("先写上收件人的专属地址吧。")
            return
        if "@" not in address:
            address = f"{address}@{POST_DOMAIN}"  # 只写前缀也认
        if not find_user(address):
            st.error("查无此人，请核对专属地址")
            return
        if address == sender:
            st.error("这封信的收件人就是你自己，换个人吧。")
            return
        is_random = 0

    # 校验都过了才落盘，免得白留一张没人认领的照片
    photo_path = save_photo(photo)
    insert_letter(
        sender,
        address,
        paper_file,
        stamp_file,
        content.strip(),
        is_random,
        photo_path,
        photo_frame if photo_path else None,
    )
    # 换一批控件 key，表单才会真正回到空白（投递方式也跟子回到默认项）
    st.session_state.pop("reply_to", None)
    st.session_state.composer += 1
    st.session_state.flash = "信件已成功寄出！"
    st.rerun()


# ---------------------------------------------------------------- 信封
def envelope_frame(hint: str = "", waiting: bool = False) -> str:
    """信封的 HTML：背面朝上、封口压着一枚火漆。素材缺失时只剩提示文字。"""
    hint_html = f'<p class="env-hint">{hint}</p>' if hint else ""
    if not ENVELOPE_PATH.exists():
        return f'<div class="envelope-anchor"></div>{hint_html}'

    uri = to_data_uri(str(ENVELOPE_PATH), 900, alpha=True)
    seal = seal_img()
    wrap = "envelope-wrap waiting" if waiting else "envelope-wrap"
    seal_html = f'<span class="envelope-seal">{seal}</span>' if seal else ""
    return (
        '<div class="envelope-anchor"></div>'
        '<div class="envelope-stage">'
        f'<div class="{wrap}">'
        f'<img class="envelope-img" src="{uri}" alt="信封">'
        f"{seal_html}"
        "</div>"
        f"{hint_html}"
        "</div>"
    )


def render_mailing() -> None:
    """投递阶段：信封居中，原生进度条约三秒走完。"""
    # 信封单独占一个槽位，进度走完时把下面那句「正在投递」换掉
    frame = st.empty()
    frame.markdown(envelope_frame("信 已 封 存 ，正 在 投 递"), unsafe_allow_html=True)

    bar = st.progress(0, text=MAIL_STEPS[0][1])
    started = time.time()
    while True:
        # 按真实经过的时间推进度：渲染快慢都不会拉长或压缩这段动画
        ratio = min((time.time() - started) / MAIL_SECONDS, 1.0)
        label = MAIL_STEPS[0][1]
        for threshold, text in MAIL_STEPS:
            if ratio >= threshold:
                label = text
        bar.progress(min(int(ratio * 100), 100), text=label)
        if ratio >= 1.0:
            break
        time.sleep(0.05)

    # 不再整页 rerun，而是就地把进度条与提示换成送达状态，动画结束到文案出现不闪断
    st.session_state.stage = "delivered"
    st.session_state.sent_at = datetime.now().strftime("%Y 年 %m 月 %d 日 %H:%M")
    frame.markdown(envelope_frame(), unsafe_allow_html=True)
    bar.progress(100, text="投 递 完 成")
    render_delivered_body()


def render_delivered() -> None:
    """送达阶段：进度条满格，给出「去收信」与「再写一封」。"""
    st.markdown(envelope_frame(), unsafe_allow_html=True)
    st.progress(100, text="投 递 完 成")
    render_delivered_body()


def render_delivered_body() -> None:
    """送达后的两个去处，投递阶段结尾与送达阶段复用。"""
    st.markdown('<p class="deliver-text">信件已送达云端邮局</p>', unsafe_allow_html=True)

    left, right = st.columns(2)
    with left:
        if st.button("去 收 信", use_container_width=True, type="primary"):
            st.session_state.stage = "unopened"
            st.rerun()
    with right:
        if st.button("再 写 一 封", use_container_width=True):
            start_new_letter()


def render_unopened() -> None:
    """收信阶段：一封尚未拆开的信，信封轻轻呼吸。"""
    st.markdown(envelope_frame("点 击 拆 开 信 件", waiting=True), unsafe_allow_html=True)
    if st.button("拆 开 信 件"):
        st.session_state.stage = "reading"
        st.rerun()


def render_letter() -> None:
    """读信阶段：纸上的原文、邮票与落款。"""
    draft = st.session_state.get("draft_text", "")
    st.markdown(f'<div class="letter-read">{html.escape(draft)}</div>', unsafe_allow_html=True)
    st.markdown(
        f'<p class="letter-sign">火漆封缄 · {st.session_state.get("sent_at", "")}</p>',
        unsafe_allow_html=True,
    )
    if st.button("再 写 一 封"):
        start_new_letter()


def start_new_letter() -> None:
    """换一张空白纸：换掉控件 key，输入框才会真正清空。"""
    st.session_state.composer += 1
    st.session_state.draft_text = ""
    st.session_state.letter_paper = None
    st.session_state.letter_stamp = None
    st.session_state.stage = "write"
    st.rerun()


# ---------------------------------------------------------------- 我的信箱
def render_mailbox() -> None:
    """我的信箱：未读在上、已读在下，点开就是一封铺好的信。"""
    user = current_user()
    letters = letters_for(user["post_address"])

    def sender_line(letter: dict) -> str:
        """「来自谁的信」；漂流瓶多贴一枚小签。"""
        badge = (
            '<span class="drift-badge">漂流瓶</span>' if letter.get("is_random") else ""
        )
        return (
            f'<p class="mail-from">来自 {html.escape(letter["sender_address"])} 的信'
            f"{badge}</p>"
        )

    # 正在看某一封：整页切成信纸
    opened_id = st.session_state.get("reading_letter")
    if opened_id is not None:
        letter = next((item for item in letters if item["id"] == opened_id), None)
        if letter:
            render_open_letter(letter)
            return
        st.session_state.reading_letter = None  # 信不在自己名下，退回列表

    unread = [item for item in letters if not item["is_read"]]
    read = [item for item in letters if item["is_read"]]

    with st.container():
        st.markdown('<div class="mailbox-anchor"></div>', unsafe_allow_html=True)

        # ---- 未读：一只信封 + 来自谁 + 拆开 ----
        st.markdown(
            f'<p class="mail-section">未 读 信 件 · 共 {len(unread)} 封</p>',
            unsafe_allow_html=True,
        )
        if unread:
            # 同一张信封图只编码一次，多封信共用
            env_uri = (
                to_data_uri(str(ENVELOPE_PATH), 300, alpha=True)
                if ENVELOPE_PATH.exists()
                else ""
            )
            for letter in unread:
                img_col, info_col = st.columns([1.0, 3.4], vertical_alignment="center")
                with img_col:
                    if env_uri:
                        st.markdown(
                            f'<img class="mail-env" src="{env_uri}" alt="未拆的信">',
                            unsafe_allow_html=True,
                        )
                with info_col:
                    st.markdown(
                        sender_line(letter)
                        + f'<p class="mail-when">{html.escape(letter["send_time"])}</p>',
                        unsafe_allow_html=True,
                    )
                    if st.button("拆 开 信 件", key=f"open_{letter['id']}"):
                        mark_read(letter["id"])  # 拆开即置为已读
                        st.session_state.reading_letter = letter["id"]
                        st.rerun()
                st.markdown('<div class="mail-rule"></div>', unsafe_allow_html=True)
        else:
            st.markdown('<p class="mail-empty">眼下没有未拆的信。</p>', unsafe_allow_html=True)

        # ---- 已读：列表，点一下再读一遍 ----
        st.markdown(
            f'<p class="mail-section">已 读 信 件 · 共 {len(read)} 封</p>',
            unsafe_allow_html=True,
        )
        if read:
            for letter in read:
                info_col, btn_col = st.columns([3.8, 1.4], vertical_alignment="center")
                with info_col:
                    preview = " ".join(letter["content"].split())
                    if len(preview) > 24:
                        preview = preview[:24] + "…"
                    st.markdown(
                        sender_line(letter)
                        + f'<p class="mail-when">{html.escape(letter["send_time"])} · '
                        f"{html.escape(preview)}</p>",
                        unsafe_allow_html=True,
                    )
                with btn_col:
                    if st.button(
                        "再 读 一 遍",
                        key=f"view_{letter['id']}",
                        use_container_width=True,
                    ):
                        st.session_state.reading_letter = letter["id"]
                        st.rerun()
                st.markdown('<div class="mail-rule"></div>', unsafe_allow_html=True)
        else:
            st.markdown('<p class="mail-empty">还没有读过的信。</p>', unsafe_allow_html=True)


def start_reply(letter: dict) -> None:
    """写回信：带着原寄件人跳回写信页；换一批控件 key，默认值才会落进去。"""
    st.session_state.reply_to = letter["sender_address"]
    st.session_state.page = "写信"
    st.session_state.reading_letter = None
    st.session_state.stage = "write"
    st.session_state.composer += 1
    st.rerun()


def render_open_letter(letter: dict) -> None:
    """拆开的一封信：以它自己的信纸为底，邮票贴在右上角。"""
    paper_path = asset_by_name(letter.get("paper"))
    stamp_path = asset_by_name(letter.get("stamp"))
    # 夹了照片就让纸变长，照片和正文才都能完整展开
    photo_rel = letter.get("photo_path")
    photo_abs = BASE_DIR / photo_rel if photo_rel else None
    has_photo = bool(photo_abs and photo_abs.exists())

    rules: list[str] = []
    floor = ""
    if paper_path:
        paper_uri, paper_w, paper_h = encode_image(paper_path, 1400)
        rules.append(f'background-image: url("{paper_uri}");')
        # 同样用最小高度：短信用原图比例，长信与夹带照片就让纸变长
        if paper_w and paper_h:
            floor = (
                f"@container (min-width: 600px) {{ {PAPER_SELECTOR} "
                f"{{ min-height: calc(600px * {paper_h} / {paper_w}); }} }}"
            )

    # 邮票按裁剪后的实际高度下压书写区，正文才不会压到邮票上
    stamp_html = ""
    if stamp_path:
        stamp_uri, stamp_w, stamp_h = encode_image(stamp_path, 420, True)
        stamp_box = STAMP_WIDTH * stamp_h / stamp_w if stamp_w else STAMP_WIDTH
        rules.append(f"padding-top: {25.6 + stamp_box + 18:.0f}px;")
        stamp_html = f'<img class="letter-stamp" src="{stamp_uri}" alt="邮票">'

    if rules or floor:
        st.markdown(
            f"<style>{PAPER_SELECTOR} {{ {' '.join(rules)} }} {floor}</style>",
            unsafe_allow_html=True,
        )

    with st.container():
        st.markdown(f'<div class="paper-anchor"></div>{stamp_html}', unsafe_allow_html=True)
        st.markdown(
            f'<div class="letter-read">{html.escape(letter["content"])}</div>',
            unsafe_allow_html=True,
        )

        # 夹带的照片：正文之下、落款之前；装了框就像相框一样立着
        if has_photo:
            frame_path = asset_by_name(letter.get("photo_frame"))
            if frame_path:
                framed, _, _ = compose_photo(str(photo_abs), frame_path, 1100)
                st.markdown(
                    f'<div class="photo-frame"><img src="{framed}"'
                    f' alt="随信附上的照片"></div>',
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    f'<div class="polaroid"><img src="{to_data_uri(str(photo_abs), 1200)}"'
                    f' alt="随信附上的照片"></div>',
                    unsafe_allow_html=True,
                )
        drift = " · 漂 流 瓶" if letter.get("is_random") else ""
        st.markdown(
            f'<p class="letter-sign">来自 {html.escape(letter["sender_address"])}{drift} · '
            f'{html.escape(letter["send_time"])}</p>',
            unsafe_allow_html=True,
        )
        back_col, reply_col = st.columns(2)
        with back_col:
            if st.button("返 回 信 箱", use_container_width=True):
                st.session_state.reading_letter = None
                st.rerun()
        with reply_col:
            if st.button("写 回 信", use_container_width=True):
                start_reply(letter)


def stamp_case_html(path: str, label: str, note: str = "") -> str:
    """陈列柜里的一枚邮票：缩略图 + 名字。"""
    uri = to_data_uri(path, 260, True)
    note_html = f"<span>{html.escape(note)}</span>" if note else ""
    return (
        '<figure class="stamp-case">'
        f'<img src="{uri}" alt="{html.escape(label)}">'
        f"<figcaption>{html.escape(label)}{note_html}</figcaption>"
        "</figure>"
    )


def render_stamps() -> None:
    """邮票陈列馆：馆藏展示 + 自制邮票（上传照片做成齿孔邮票）。"""
    user = current_user()
    address = user["post_address"]
    st.markdown(
        '<div class="gallery-head"><h3>邮票陈列馆</h3>'
        "<p>方寸之间，藏着旧年的山河与人事。</p></div>",
        unsafe_allow_html=True,
    )

    mine_tab, builtin_tab = st.tabs(["我的邮票", "馆藏邮票"])

    with builtin_tab:
        builtin = list_prefixed("stamp_")
        if builtin:
            cases = "".join(
                stamp_case_html(path, f"邮票 {index:02d}")
                for index, path in enumerate(builtin, 1)
            )
            st.markdown(f'<div class="stamp-grid">{cases}</div>', unsafe_allow_html=True)
            st.caption("写信时在「选择邮票」里就能贴上这些馆藏。")
        else:
            st.info("assets/ 下还没有 stamp_*.png")

    with mine_tab:
        notice = st.session_state.pop("stamp_notice", "")
        if notice:
            st.success(notice)

        mine = custom_stamps_for(address)
        if mine:
            columns = st.columns(4)
            for index, stamp in enumerate(mine):
                with columns[index % 4]:
                    path = stamp_path_by_name(stamp["filename"])
                    if path:
                        st.markdown(
                            '<div class="stamp-preview"><img '
                            f'src="{to_data_uri(path, 320, True)}" '
                            'alt="我的邮票"></div>',
                            unsafe_allow_html=True,
                        )
                    st.caption(f"{stamp['name']} · {stamp['created_at'][:10]}")
                    if stamp_used_in_letters(stamp["filename"]):
                        st.caption("已贴在寄出的信上，取不下来")
                    elif st.button("取 下", key=f"del_stamp_{stamp['id']}"):
                        if delete_custom_stamp(stamp["id"], address):
                            st.session_state.stamp_notice = (
                                f"「{stamp['name']}」已从集邮册取下。"
                            )
                            st.rerun()
        else:
            st.caption("还没有自己的邮票。上传一张照片，就能把它做成方寸之间的珍藏。")

        st.markdown('<p class="gallery-label">制 作 我 的 邮 票</p>', unsafe_allow_html=True)
        if Image is None:
            st.warning("缺少 Pillow，暂时无法制作邮票。")
            return

        epoch = st.session_state.get("stamp_epoch", 0)
        upload = st.file_uploader(
            "■上传一张照片做成邮票(支持jpg/png,从相册或电脑选择)",
            type=["jpg", "jpeg", "png"],
            accept_multiple_files=False,
            key=f"stamp_upload_{epoch}",
        )
        name = st.text_input(
            "给这枚邮票起个名字",
            max_chars=12,
            placeholder="例如：旧年夏天",
            key=f"stamp_name_{epoch}",
        )
        if upload is not None:
            st.markdown(
                '<div class="stamp-preview large"><img '
                f'src="{stamp_preview_uri(upload.getvalue())}" '
                'alt="邮票预览"></div>',
                unsafe_allow_html=True,
            )
            st.caption("照片会被居中裁进白边里，四周打上一圈齿孔。")

        full = len(mine) >= MAX_MY_STAMPS
        if full:
            st.caption(f"集邮册最多收 {MAX_MY_STAMPS} 枚自制邮票，先取下几枚吧。")
        if st.button("制 作 这 枚 邮 票", disabled=upload is None or full):
            if upload is not None:
                title = name.strip() or "无名小票"
                if save_custom_stamp(upload, address, title):
                    st.session_state.stamp_notice = (
                        f"「{title}」已收进你的集邮册，写信时就能贴上。"
                    )
                    st.session_state.stamp_epoch = epoch + 1
                    st.rerun()
                else:
                    st.warning("这张照片没能做成邮票，换一张试试。")


def render_gate() -> None:
    """未登录：主区只留一张提示卡。"""
    notice = st.session_state.pop("gate_notice", "")
    notice_html = f'<p class="gate-sub">{html.escape(notice)}</p>' if notice else ""
    st.markdown(
        '<div class="gate-card">'
        f'<div class="gate-seal">{seal_img()}</div>'
        '<p class="gate-text">请先在左侧注册或登录您的时光邮局账户</p>'
        '<p class="gate-sub">信写得再长，也得先有个寄得出去的地址。</p>'
        f"{notice_html}"
        "</div>",
        unsafe_allow_html=True,
    )


def render_welcome(user: dict) -> None:
    """已登录：报头下方的一行欢迎条；地址默认打码。"""
    address = user["post_address"]
    epoch = st.session_state.get("auth_epoch", 0)
    if not st.session_state.get(f"show_address_{epoch}"):
        address = mask_address(address)
    st.markdown(
        '<div class="welcome-bar">'
        '<p class="welcome-text">欢迎回来，您的专属地址是：'
        f'<span class="welcome-addr">{html.escape(address)}</span></p>'
        "</div>",
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------- 侧边栏组件
def render_account(user: dict) -> None:
    """已登录：账户卡 + 隐私与安全（地址遮罩、漂流瓶开关、改密码、注销）。"""
    address = user["post_address"]
    epoch = st.session_state.get("auth_epoch", 0)
    shown = address if st.session_state.get(f"show_address_{epoch}") else mask_address(address)
    st.markdown(
        '<div class="account-card">'
        f'<p class="account-name">{html.escape(user["username"])}</p>'
        f'<p class="account-addr">{html.escape(shown)}</p>'
        "</div>",
        unsafe_allow_html=True,
    )
    st.checkbox("显示完整地址", key=f"show_address_{epoch}")

    with st.expander("隐私与安全"):
        allow = st.checkbox(
            "愿意收到漂流瓶",
            value=bool(user.get("allow_random", 1)),
            help="关掉后，别人随缘投递时不会再抽到你",
        )
        if allow != bool(user.get("allow_random", 1)):
            set_allow_random(address, allow)
            st.session_state.user = find_user(address)
            st.success("设置已保存。")

        st.markdown('<p class="side-note">修 改 密 码</p>', unsafe_allow_html=True)
        with st.form("change_password_form"):
            old = st.text_input("当前密码", type="password")
            new = st.text_input("新密码", type="password")
            again = st.text_input("再输一遍新密码", type="password")
            if st.form_submit_button("修 改 密 码", use_container_width=True):
                fresh = find_user(address) or {}
                if not verify_password(old, fresh.get("password_hash")):
                    st.error("当前密码不对。")
                elif len(new) < PASSWORD_MIN:
                    st.error(f"新密码至少 {PASSWORD_MIN} 位。")
                elif new != again:
                    st.error("两次输入不一致。")
                else:
                    set_password(address, new)
                    st.success("密码已更新，下次用新密码登录。")

        st.markdown('<p class="side-note">注 销 账 户</p>', unsafe_allow_html=True)
        st.caption("注销后，您收发的信与夹带的照片会一起销毁，无法找回。")
        sure = st.checkbox("我明白这一点", key=f"confirm_delete_{epoch}")
        if st.button(
            "注 销 并 删 除 一 切", disabled=not sure, key=f"delete_account_{epoch}"
        ):
            removed = delete_account(address)
            sign_out()
            st.session_state.gate_notice = f"账户已注销，{removed} 封信随之销毁。"
            st.rerun()

    if st.button("退 出 登 录", key="sign_out"):
        sign_out()
        st.rerun()


def render_password_setup(address: str) -> None:
    """老账户第一次登录：先给它设一个密码，之后才能进门。"""
    epoch = st.session_state.get("auth_epoch", 0)
    st.caption(f"「{address}」还没有设置密码，先设一个，以后凭密码登录。")
    with st.form("setup_password_form"):
        first = st.text_input("设置密码", type="password", key=f"setup_pw_{epoch}")
        second = st.text_input("再输一遍", type="password", key=f"setup_pw2_{epoch}")
        if st.form_submit_button("保 存 并 登 录", use_container_width=True):
            if len(first) < PASSWORD_MIN:
                st.error(f"密码至少 {PASSWORD_MIN} 位。")
            elif first != second:
                st.error("两次输入不一致。")
            else:
                set_password(address, first)
                st.session_state.pop("pending_password", None)
                user = find_user(address)
                if user:
                    sign_in(user)
                    st.rerun()
    if st.button("换一个地址", key="cancel_setup"):
        st.session_state.pop("pending_password", None)
        st.rerun()


def render_auth_forms() -> None:
    """未登录：登录 / 注册两个表单；密码只存摘要，不落明文。"""
    epoch = st.session_state.get("auth_epoch", 0)
    login_tab, register_tab = st.tabs(["登 录", "注 册"])

    with login_tab:
        pending = st.session_state.get("pending_password")
        if pending:
            render_password_setup(pending)
        else:
            with st.form("login_form"):
                address = st.text_input(
                    "专属地址",
                    placeholder=f"xiaoming@{POST_DOMAIN}",
                    key=f"login_address_{epoch}",
                )
                password = st.text_input(
                    "密码", type="password", key=f"login_password_{epoch}"
                )
                if st.form_submit_button("登 录 我 的 信 箱"):
                    typed = normalize_prefix(address)
                    if typed and "@" not in typed:
                        typed = f"{typed}@{POST_DOMAIN}"  # 只写前缀也认
                    user = find_user(typed) if typed else None
                    if not user or not verify_password(
                        password, user.get("password_hash")
                    ):
                        # 不区分「地址不存在」和「密码不对」，少漏一点信息
                        if user and not user.get("password_hash"):
                            st.session_state.pending_password = typed
                            st.rerun()
                        st.error("地址或密码不对。")
                    else:
                        sign_in(user)
                        st.rerun()

    with register_tab:
        with st.form("register_form"):
            username = st.text_input(
                "您的称呼", placeholder="林见鹿", key=f"reg_name_{epoch}"
            )
            prefix = st.text_input(
                "地址前缀", placeholder="xiaoming", key=f"reg_prefix_{epoch}"
            )
            password = st.text_input(
                "设置密码", type="password", key=f"reg_pw_{epoch}"
            )
            confirm = st.text_input(
                "再输一遍密码", type="password", key=f"reg_pw2_{epoch}"
            )
            st.caption(f"注册后地址形如：前缀@{POST_DOMAIN}；密码只存加盐摘要")
            if st.form_submit_button("注 册 并 入 住"):
                cleaned = normalize_prefix(prefix)
                if not username.strip():
                    st.error("请先留个称呼。")
                elif not PREFIX_PATTERN.fullmatch(cleaned):
                    st.error("前缀需 3-20 位，用小写字母、数字或 . _ - 组成。")
                elif len(password) < PASSWORD_MIN:
                    st.error(f"密码至少 {PASSWORD_MIN} 位。")
                elif password != confirm:
                    st.error("两次输入不一致。")
                else:
                    new_address = f"{cleaned}@{POST_DOMAIN}"
                    if find_user(new_address):
                        st.error(f"「{cleaned}」已被占用，换一个吧。")
                    else:
                        try:
                            user = create_user(username.strip(), new_address, password)
                        except sqlite3.IntegrityError:
                            st.error(f"「{cleaned}」已被占用，换一个吧。")
                        else:
                            sign_in(user)
                            st.rerun()


# ---------------------------------------------------------------- 侧边栏
if "page" not in st.session_state:
    st.session_state.page = "写信"
if "stage" not in st.session_state:
    st.session_state.stage = "write"
if "composer" not in st.session_state:
    st.session_state.composer = 0
if "reading_letter" not in st.session_state:
    st.session_state.reading_letter = None
if "auth_epoch" not in st.session_state:
    st.session_state.auth_epoch = 0

# 静置太久自动上锁：共享电脑上人走开也不会把信箱敲着
if current_user():
    now = time.time()
    last = st.session_state.get("last_active", now)
    if now - last > IDLE_LOCK_SECONDS:
        sign_out()
        st.session_state.gate_notice = "信箱静置太久，已自动上锁，请重新输入密码。"
    else:
        st.session_state.last_active = now

with st.sidebar:
    st.markdown('<div class="toolbox-plate">见字如晤 · 工具箱</div>', unsafe_allow_html=True)

    st.markdown('<div class="drawer-label">账 户</div>', unsafe_allow_html=True)
    user = current_user()
    if user:
        render_account(user)
    else:
        render_auth_forms()

    # 没登录就不铺书信导航，免得点进去也只有一张提示卡
    if user:
        st.markdown('<div class="drawer-label">书 信</div>', unsafe_allow_html=True)
        for item in NAV_ITEMS:
            marker = "▸ " if st.session_state.page == item else "　 "
            if st.button(f"{marker}{item}", key=f"nav_{item}"):
                st.session_state.page = item
                st.session_state.reading_letter = None  # 换页就合上手里那封
                st.session_state.pop("reply_to", None)  # 手点的导航，不带回信上下文
                st.rerun()

    st.markdown('<div class="toolbox-handle"></div>', unsafe_allow_html=True)
    st.caption("愿每一封信，都有归处。")

# ---------------------------------------------------------------- 主页面
render_header()

if not current_user():
    render_gate()
else:
    render_welcome(current_user())
    if st.session_state.page == "写信":
        # 写信 → 投递 → 送达 → 待拆 → 读信，全流程由 stage 串起来
        stage = st.session_state.stage
        if stage == "mailing":
            render_mailing()
        elif stage == "delivered":
            render_delivered()
        elif stage == "unopened":
            render_unopened()
        else:
            render_write()
    elif st.session_state.page == "我的信箱":
        render_mailbox()
    else:
        render_stamps()

st.markdown(
    '<div class="letter-footer">见字如晤 · 时光邮局　|　纸短情长，不尽依依</div>',
    unsafe_allow_html=True,
)
