#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
安装包索引管家 2.2.2
"""

import os
import sys
import subprocess
import json
import re
import csv
import logging
import threading
from datetime import datetime

APP_NAME = "安装包索引管家"
APP_VERSION = "2.2.2"


def install_and_import(package, import_name=None):
    if import_name is None:
        import_name = package
    try:
        __import__(import_name)
        return
    except ImportError:
        pass
    if getattr(sys, "frozen", False):
        # 打包后的 exe 里 sys.executable 是 exe 自身，不能再调 pip
        raise ImportError(
            f"缺少依赖 {package}（import 名 {import_name}），"
            f"请在打包环境中安装后重新打包。"
        )
    print(f"正在安装缺失的依赖库: {package} ...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", package,
                           "-i", "https://pypi.tuna.tsinghua.edu.cn/simple"])


install_and_import("py7zr")
install_and_import("rarfile")
install_and_import("tkinterdnd2", "tkinterdnd2")

import sqlite3
import hashlib
import zipfile
import py7zr
import rarfile
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from tkinterdnd2 import TkinterDnD, DND_FILES
import ctypes
import ctypes.wintypes

if getattr(sys, 'frozen', False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))


def resource_path(name):
    """兼容源码运行和 onefile 打包：找随程序分发的资源（如 app.ico）。"""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, name)


DB_PATH = os.path.join(APP_DIR, "pack_index.db")

LOG_PATH = os.path.join(APP_DIR, "packmanager.log")


def db_connect(timeout=30.0):
    """统一的 SQLite 连接：WAL + busy_timeout，避免后台扫描把界面查询锁死。"""
    con = sqlite3.connect(DB_PATH, timeout=timeout)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA busy_timeout=30000")
    except sqlite3.Error:
        pass
    return con

PACK_EXT = {
    ".exe", ".msi", ".msix", ".appx",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz",
    ".iso", ".img", ".dmg", ".pkg",
    ".apk", ".jar"
}

SKIP_DIRS = {"$recycle.bin", "system volume information",
             "node_modules", ".git", "__pycache__"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, name TEXT NOT NULL, ext TEXT,
  size INTEGER, mtime REAL, atime REAL, sha1 TEXT, product TEXT, description TEXT,
  version TEXT, company TEXT, inner TEXT, notes TEXT, tags TEXT
);
CREATE INDEX IF NOT EXISTS idx_name  ON files(name);
CREATE INDEX IF NOT EXISTS idx_sha1  ON files(sha1);
"""

LOG = logging.getLogger("packmanager")


def setup_logging():
    try:
        from logging.handlers import RotatingFileHandler
        handler = RotatingFileHandler(LOG_PATH, maxBytes=1 << 20, backupCount=3,
                                      encoding="utf-8")
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            handlers=[handler])
    except Exception:
        pass


# ==================== 工具 ====================
def clean_text(txt):
    if txt is None:
        return ""
    txt = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', str(txt))
    txt = txt.replace('□', '').replace('"', '').strip()
    return txt


def like_escape(s):
    """转义 SQLite LIKE 的通配符（配合 ESCAPE '!' 使用）。

    文件名里的 _ 和 % 很常见（如 cpu-z_2.17-cn.zip），不转义会被当成通配符，
    导致搜索/导入匹配到无关记录。
    """
    return str(s).replace("!", "!!").replace("%", "!%").replace("_", "!_")


def fmt_time(ts):
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def fmt_size(n):
    try:
        n = int(n or 0)
    except Exception:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


def sha1_file(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


# ==================== 元数据提取 ====================
def file_version_info(path):
    if os.name != "nt":
        return {}
    try:
        ver = ctypes.WinDLL("version")
        size = ver.GetFileVersionInfoSizeW(path, None)
        if not size:
            return {}
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(path, 0, size, buf):
            return {}
        p, n = ctypes.c_void_p(), ctypes.c_uint()
        if not ver.VerQueryValueW(buf, "\\VarFileInfo\\Translation",
                                   ctypes.byref(p), ctypes.byref(n)):
            return {}
        lang, cp = ctypes.cast(p, ctypes.POINTER(ctypes.c_ushort * 2)).contents
        out = {}
        for key in ("ProductName", "FileDescription", "FileVersion",
                    "ProductVersion", "CompanyName"):
            sub = f"\\StringFileInfo\\{lang:04x}{cp:04x}\\{key}"
            p2, n2 = ctypes.c_void_p(), ctypes.c_uint()
            if ver.VerQueryValueW(buf, sub, ctypes.byref(p2), ctypes.byref(n2)) and n2.value:
                out[key] = ctypes.wstring_at(p2.value, n2.value).rstrip("\x00")
        return out
    except Exception:
        return {}


def archive_listing(path, limit=50):
    names = []
    try:
        if path.lower().endswith(".zip"):
            with zipfile.ZipFile(path) as z:
                for info in z.infolist():
                    if info.is_dir():
                        continue
                    n = info.filename
                    if not (info.flag_bits & 0x800):
                        try:
                            n = n.encode("cp437").decode("gbk")
                        except Exception:
                            pass
                    names.append(os.path.basename(n))
        elif path.lower().endswith(".7z"):
            with py7zr.SevenZipFile(path, mode='r') as z:
                for n in z.getnames():
                    names.append(os.path.basename(n))
        elif path.lower().endswith(".rar"):
            with rarfile.RarFile(path) as z:
                for n in z.namelist():
                    names.append(os.path.basename(n))
        names = [n for n in names if n]
        return " | ".join(names[:limit])
    except Exception:
        return None


# ==================== DB ====================
def upsert_file(cur, p):
    try:
        st = os.stat(p)
    except OSError:
        return False

    cur.execute("SELECT size, mtime FROM files WHERE path=?", (p,))
    row = cur.fetchone()
    if row and row[0] == st.st_size and abs(row[1] - st.st_mtime) < 2:
        cur.execute("UPDATE files SET atime=? WHERE path=?", (st.st_atime, p))
        return False

    ext = os.path.splitext(p)[1].lower()
    meta, inner = {}, None
    if ext in (".exe", ".msi", ".dll"):
        meta = file_version_info(p)
    elif ext in (".zip", ".7z", ".rar"):
        inner = archive_listing(p)

    cur.execute("""
        INSERT INTO files(path,name,ext,size,mtime,atime,product,description,version,company,inner)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(path) DO UPDATE SET name=excluded.name, ext=excluded.ext, size=excluded.size,
          mtime=excluded.mtime, atime=excluded.atime, product=excluded.product,
          description=excluded.description, version=excluded.version, company=excluded.company,
          inner=excluded.inner, sha1=NULL
    """, (p, os.path.basename(p), ext, st.st_size, st.st_mtime, st.st_atime,
          meta.get("ProductName"), meta.get("FileDescription"),
          meta.get("FileVersion") or meta.get("ProductVersion"),
          meta.get("CompanyName"), inner))
    return True


def iter_packs(roots):
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if d.lower() not in SKIP_DIRS]
            for fn in fns:
                if os.path.splitext(fn)[1].lower() in PACK_EXT:
                    yield os.path.join(dp, fn)


def scan(roots, progress=None, log=None):
    con = db_connect()
    cur = con.cursor()
    cur.executescript(SCHEMA)

    LOG.info("scan start roots=%s", roots)
    # roots 可能互相嵌套（如 H:\扫描 与 H:\扫描\PackManager 同时存在），去重避免重复 upsert
    paths = list(dict.fromkeys(iter_packs(roots)))
    total = len(paths)
    newset = set(paths)

    for i, p in enumerate(paths):
        if progress and (i % 10 == 0 or i == total - 1):
            progress(i + 1, total, p)
        try:
            upsert_file(cur, p)
        except Exception as e:
            LOG.warning("upsert failed %s: %s", p, e)
        if i % 50 == 0:
            con.commit()

    roots_n = [os.path.normcase(os.path.abspath(r)) for r in roots]
    stale = []
    for (p,) in cur.execute("SELECT path FROM files"):
        if p in newset:
            continue
        pn = os.path.normcase(os.path.abspath(p))
        if any(pn == r or pn.startswith(r + os.sep) for r in roots_n):
            stale.append(p)
    if stale:
        cur.executemany("DELETE FROM files WHERE path=?", [(p,) for p in stale])
    con.commit()

    if log:
        log("正在进行重复文件比对...")
    sizes = [r[0] for r in cur.execute(
        "SELECT size FROM files WHERE sha1 IS NULL GROUP BY size HAVING COUNT(*)>1")]
    for size in sizes:
        ids = cur.execute(
            "SELECT id,path FROM files WHERE size=? AND sha1 IS NULL", (size,)).fetchall()
        for fid, p in ids:
            try:
                cur.execute("UPDATE files SET sha1=? WHERE id=?", (sha1_file(p), fid))
            except OSError:
                pass
        con.commit()
    con.close()
    LOG.info("scan done total=%d", total)


SORT_MAP = {"name": "f.name", "size": "f.size", "version": "f.version",
            "mtime": "f.mtime", "desc": "f.description", "notes": "f.notes",
            "path": "f.path", "tags": "f.tags"}


def _to_float(txt):
    """空串/非法值都返回 None，由调用方决定忽略单个条件。"""
    txt = (txt or "").strip()
    if not txt:
        return None
    try:
        return float(txt)
    except ValueError:
        return None


def _to_day_ts(txt, end_of_day=False):
    """把 YYYY-MM-DD 转成当天 00:00 的时间戳；end_of_day=True 时取当天 23:59:59。"""
    txt = (txt or "").strip()
    if not txt:
        return None
    try:
        dt = datetime.strptime(txt, "%Y-%m-%d")
    except ValueError:
        return None
    ts = dt.timestamp()
    return ts + 86399 if end_of_day else ts


def build_query_where(q, size_min, size_max, size_unit, time_min, time_max, path_filter):
    where, params = [], []
    for term in q.split():
        like = f"%{like_escape(term)}%"
        where.append("(f.name LIKE ? ESCAPE '!' OR f.product LIKE ? ESCAPE '!' "
                     "OR f.description LIKE ? ESCAPE '!' OR f.inner LIKE ? ESCAPE '!' "
                     "OR f.notes LIKE ? ESCAPE '!' OR f.tags LIKE ? ESCAPE '!' "
                     "OR f.path LIKE ? ESCAPE '!')")
        params += [like] * 7
    if path_filter.strip():
        cp = path_filter.strip().replace('"', '').replace("\\", "/")
        where.append("REPLACE(f.path, '\\', '/') LIKE ? ESCAPE '!'")
        params.append(f"%{like_escape(cp)}%")
    mult = 1024 * 1024 if size_unit == "MB" else 1024 * 1024 * 1024
    v1 = _to_float(size_min)
    v2 = _to_float(size_max)
    v1 = int(v1 * mult) if v1 is not None and v1 > 0 else None
    v2 = int(v2 * mult) if v2 is not None and v2 > 0 else None
    if v1 is not None and v2 is not None and v1 > v2:
        v1, v2 = v2, v1
    if v1 is not None and v2 is not None:
        where.append(f"f.size BETWEEN {v1} AND {v2}")
    elif v1 is not None:
        where.append(f"f.size >= {v1}")
    elif v2 is not None:
        where.append(f"f.size <= {v2}")
    ts1 = _to_day_ts(time_min)
    ts2 = _to_day_ts(time_max, end_of_day=True)
    if ts1 is not None and ts2 is not None and ts1 > ts2:
        ts1, ts2 = ts2 - 86399, ts1 + 86399
    if ts1 is not None and ts2 is not None:
        where.append(f"f.mtime BETWEEN {ts1} AND {ts2}")
    elif ts1 is not None:
        where.append(f"f.mtime >= {ts1}")
    elif ts2 is not None:
        where.append(f"f.mtime <= {ts2}")
    return where, params


def fetch_page(q="", sort_col="mtime", sort_desc=True, page=0, page_size=500,
               size_min="", size_max="", size_unit="MB",
               time_min="", time_max="", path_filter=""):
    """一次性返回 (总数, 实际页码, 当前页数据)，避免 count/query 重复计算 where。"""
    con = db_connect()
    cur = con.cursor()
    cur.executescript(SCHEMA)
    where, params = build_query_where(q, size_min, size_max, size_unit,
                                      time_min, time_max, path_filter)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    total = cur.execute("SELECT COUNT(*) FROM files f" + where_sql, params).fetchone()[0]
    maxp = max(0, (total - 1) // page_size) if total else 0
    page = min(page, maxp)
    col = SORT_MAP.get(sort_col, "f.mtime")
    sql = ("SELECT f.id,f.name,f.ext,f.size,f.version,f.product,f.description,"
           "f.mtime,f.path,f.notes,f.tags FROM files f" + where_sql +
           f" ORDER BY {col} {'DESC' if sort_desc else 'ASC'} "
           f"LIMIT {page_size} OFFSET {page * page_size}")
    rows = cur.execute(sql, params).fetchall()
    con.close()
    return total, page, rows


# ==================== 已安装软件检测 ====================
def get_installed_software():
    """列出已安装软件名：HKLM/HKCU 各按 64 位与 32 位注册表视图各扫一遍。

    旧版只扫 HKLM，导致「仅为我安装」的软件（Chrome、VS Code、各类绿色安装器）
    全部漏检，软件视图里明明装了也显示“未安装”。
    """
    if os.name != 'nt':
        return []
    try:
        import winreg
    except ImportError:
        return []

    see64 = getattr(winreg, "KEY_WOW64_64KEY", 0)
    see32 = getattr(winreg, "KEY_WOW64_32KEY", 0)
    views = [(winreg.KEY_READ | see64)]
    if see32 and see32 != see64:
        views.append(winreg.KEY_READ | see32)
    uninstall = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"

    installed, seen = [], set()
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for access in views:
            try:
                with winreg.OpenKey(hive, uninstall, 0, access) as key:
                    count = winreg.QueryInfoKey(key)[0]
                    for i in range(count):
                        try:
                            subkey_name = winreg.EnumKey(key, i)
                            with winreg.OpenKey(key, subkey_name) as subkey:
                                try:
                                    display_name, _ = winreg.QueryValueEx(
                                        subkey, "DisplayName")
                                except OSError:
                                    continue
                                name = (display_name or "").strip()
                                if not name or name.lower() in seen:
                                    continue
                                seen.add(name.lower())
                                installed.append(name)
                        except OSError:
                            continue
            except OSError:
                continue
    return installed


_PUNCT = re.compile(r'[\s\-_\.\,\(\)（）\[\]【】]+')

_STEM_TAIL_KEYS = ('setup', 'installer', 'install', 'portable', 'green', 'repack',
                   'beta', 'alpha', 'rc', '预览版', '绿色版', '便携版',
                   '安装包', '安装版', '免安装')


def _norm_sw(s):
    return _PUNCT.sub('', str(s or '').lower())


_STEM_DIGITS = re.compile(r'^(.*?[^\W\d_]{3,})\d+$')


def _stem(s):
    """比 _norm_sw 更宽松：再砍掉结尾的安装器词与版本/编号段，用于和注册表名比对。"""
    name = normalize_software_name(str(s or ''))
    tokens = [t for t in name.replace('-', ' ').replace('_', ' ').split() if t]
    while len(tokens) > 1:
        last = tokens[-1]
        if last in _STEM_TAIL_KEYS or any(ch.isdigit() for ch in last):
            tokens.pop()
        else:
            break
    out = _norm_sw(' '.join(tokens))
    # 单个词也常把版本号粘在名字后面（snipaste2 / wps2019），去掉尾巴数字
    m = _STEM_DIGITS.match(out)
    if m:
        out = m.group(1)
    return out or _norm_sw(name)


def match_installed(name, product, installed_norm):
    """判断文件是否对应某个已安装软件。

    installed_norm 里混放了已安装名的 _norm_sw 与 _stem 两种形态，
    这里对 name/product 也生成两种形态比对，修掉「注册表叫 WinRAR、
    文件叫 winrar-x64-624scp.exe 就判未安装」这类假阴性。
    """
    if not installed_norm:
        return False
    short_sws = [sw for sw in installed_norm if len(sw) >= 4]
    for raw in (product, name):
        if not raw:
            continue
        cands = []
        for fn in (_norm_sw, _stem):
            try:
                v = fn(raw)
            except Exception:
                v = ""
            if v and len(v) >= 4 and v not in cands:
                cands.append(v)
        for n in cands:
            if n in installed_norm:
                return True
            for sw in short_sws:
                short, long_ = (n, sw) if len(n) <= len(sw) else (sw, n)
                if short in long_ and len(short) >= max(4, int(len(long_) * 0.6)):
                    return True
    return False


# ==================== 软件级聚合 ====================
_VER_PAT     = re.compile(r'(?i)[\s_\-\.]*(?:v|ver|version)?\s*\d+(?:[\.\-_]\d+){1,4}(?:[\-_][a-z0-9]+)*')
_ARCH_PAT    = re.compile(r'(?i)[\s_\-\.]*(x86|x64|amd64|arm64|win32|win64|32bit|64bit|32位|64位)')
_LANG_PAT    = re.compile(r'(?i)[\s_\-\.]*(chs|cht|zh-cn|zh_cn|zh-tw|简体中文|繁体中文|中文版|多语言)')
_TAIL_PAT    = re.compile(r'(?i)[\s_\-\.]*(setup|install(?:er)?|portable|green|repack|绿色版|便携版|安装包|安装版|免安装)')
_BRACKET_PAT = re.compile(r'[\(（\[【][^\)）\]】]{1,30}[\)）\]】]')


def normalize_software_name(fname):
    name = os.path.splitext(fname)[0]
    name = _BRACKET_PAT.sub(' ', name)
    name = _VER_PAT.sub(' ', name)
    name = _ARCH_PAT.sub(' ', name)
    name = _LANG_PAT.sub(' ', name)
    name = _TAIL_PAT.sub(' ', name)
    name = re.sub(r'[\s_\-\.]+', ' ', name).strip().lower()
    return name or os.path.splitext(fname)[0].lower()


def parse_version(v):
    if not v:
        return ()
    nums = re.findall(r'\d+', str(v))
    return tuple(int(x) for x in nums[:6]) if nums else ()


def detect_pe_installer_type(path):
    try:
        if not path.lower().endswith(('.exe', '.msi')):
            return None
        size = os.path.getsize(path)
        if size < 4096:
            return None
        with open(path, "rb") as f:
            f.seek(max(0, size - 512 * 1024))
            tail = f.read()
        if b'Inno Setup Setup Data' in tail or b'Inno Setup' in tail:
            return 'Inno Setup'
        if b'NullsoftInst' in tail or b'Nullsoft Install System' in tail:
            return 'NSIS'
        return None
    except Exception:
        return None


def detect_installer_type(path, ext, inner=None, product=None, description=None):
    e = (ext or '').lower()
    if e in ('.msi', '.msix', '.appx'):
        return 'MSI'
    if e == '.apk':
        return 'APK'
    if e == '.jar':
        return 'JAR'
    if e == '.exe':
        pe = detect_pe_installer_type(path)
        if pe:
            return pe
    d = f"{product or ''} {description or ''}".lower()
    if 'inno setup' in d:
        return 'Inno Setup'
    if 'nsis' in d:
        return 'NSIS'
    if e in ('.zip', '.7z', '.rar', '.tar', '.gz', '.tgz', '.bz2', '.xz'):
        if inner:
            files = [x.strip() for x in str(inner).split('|') if x.strip()]
            if len(files) <= 6 and any(f.lower().endswith('.exe') for f in files):
                joined = ' '.join(files).lower()
                if not any(k in joined for k in ('setup', 'install', '安装')):
                    return '绿色版'
        return '压缩包'
    if e in ('.exe', '.img', '.iso', '.dmg', '.pkg'):
        return '普通安装包'
    return '未知'


_TYPE_CACHE = {}
_TYPE_CACHE_MAX = 20000


def detect_installer_type_cached(path, ext, inner=None, product=None, description=None):
    """识别结果按 (path, size, mtime) 缓存。

    detect_installer_type 对 exe 要读文件尾部 512KB，软件视图每次刷新都要把所有
    文件过一遍，机械盘上能卡好几秒；识别结果只跟这几个字段有关，缓存即可。
    """
    try:
        st = os.stat(path)
        key = (path, st.st_size, int(st.st_mtime))
    except OSError:
        return detect_installer_type(path, ext, inner, product, description)
    hit = _TYPE_CACHE.get(key)
    if hit is not None:
        return hit
    val = detect_installer_type(path, ext, inner, product, description)
    if len(_TYPE_CACHE) >= _TYPE_CACHE_MAX:
        _TYPE_CACHE.clear()
    _TYPE_CACHE[key] = val
    return val


_GENERIC_PRODUCTS = {"setup", "install", "installer", "installation", "install shield",
                     "program", "application", "app", "update", "安装程序", "安装包",
                     "unknown", "未知", "无"}


def _group_key(name, product):
    """软件视图的分组键。

    优先用 Win32 版本信息里的 ProductName：官方安装包的文件名常常只有版本号
    （7z2501-x64.exe、node-v22.11.0-x64.msi），只按文件名归一化会把同一个软件
    拆成好几组；没有产品名或产品名没信息量（"Setup"）时退回文件名。
    """
    if product:
        pkey = normalize_software_name(product)
        if len(pkey) >= 3 and pkey not in _GENERIC_PRODUCTS:
            return "p:" + pkey
    return "n:" + normalize_software_name(name)


def query_software_groups(q="", size_min="", size_max="", size_unit="MB",
                          time_min="", time_max="", path_filter=""):
    con = db_connect()
    cur = con.cursor()
    cur.executescript(SCHEMA)
    where, params = build_query_where(q, size_min, size_max, size_unit, time_min, time_max, path_filter)
    sql = ("SELECT f.id,f.name,f.ext,f.size,f.version,f.product,f.description,"
           "f.mtime,f.path,f.notes,f.tags,f.inner FROM files f")
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = cur.execute(sql, params).fetchall()
    con.close()

    groups = {}
    for r in rows:
        (_id, name, ext, size, version, product, description, mtime, path,
         notes, tags, inner) = r
        key = _group_key(name, product)
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "key": key, "display": None, "versions": [], "ids": [],
                "names": set(), "products": set(), "paths": [],
                "sizes": [], "mtimes": [], "tags": set(), "types": set(),
                "latest": None,
            }
        g["ids"].append(_id)
        g["names"].add(name)
        if product:
            g["products"].add(product)
        g["paths"].append(path)
        g["sizes"].append(size or 0)
        if mtime:
            g["mtimes"].append(mtime)
        if tags:
            g["tags"].update(t.strip() for t in str(tags).split(',') if t.strip())
        g["types"].add(detect_installer_type_cached(path, ext, inner, product, description))
        g["versions"].append((parse_version(version), r))

    out = []
    for g in groups.values():
        g["versions"].sort(key=lambda x: (x[0], x[1][7] or 0), reverse=True)
        g["latest"] = g["versions"][0][1]
        pool = g["products"] or g["names"]
        g["display"] = (sorted(pool, key=lambda x: (len(x), x))[0]
                        if pool else g["key"].split(":", 1)[-1])
        out.append(g)
    out.sort(key=lambda g: (g["latest"][7] or 0), reverse=True)
    return out


def export_tags_rows(cur):
    """导出所有带标签/备注的记录。

    附带 path 与 size：只有 sha1 时（老库里大部分 sha1 为 NULL，因为只对同 size
    重复文件才计算）恢复时几乎匹配不上，只能退回模糊文件名匹配。
    """
    rows = cur.execute(
        "SELECT sha1,name,tags,notes,path,size FROM files "
        "WHERE (tags IS NOT NULL AND tags!='') "
        "OR (notes IS NOT NULL AND notes!='') ORDER BY name").fetchall()
    return [{"sha1": r[0], "name": r[1], "tags": r[2], "notes": r[3],
             "path": r[4], "size": r[5]} for r in rows]


def restore_tags(cur, data):
    """把导出的标签/备注写回数据库，返回 (成功数, 跳过数)。

    匹配顺序：sha1 → 完整路径 → (文件名, 大小) → 文件名精确 → 唯一模糊命中。
    旧版本用 `name LIKE '%x%'` 直接 UPDATE，匹配不到就静默丢、匹配多了就把
    同一批标签糊到一堆无关记录上，这里改成必须定位到唯一一行才写。
    """
    ok = skipped = 0
    for item in data or []:
        if not isinstance(item, dict):
            skipped += 1
            continue
        tags = item.get("tags") or ""
        notes = item.get("notes") or ""
        if not str(tags).strip() and not str(notes).strip():
            skipped += 1
            continue
        sha1 = str(item.get("sha1") or "").strip()
        name = str(item.get("name") or "").strip()
        path = str(item.get("path") or "").strip()
        size = item.get("size")
        target = None
        if sha1:
            target = cur.execute(
                "SELECT id FROM files WHERE sha1=? LIMIT 1", (sha1,)).fetchone()
        if not target and path:
            target = cur.execute(
                "SELECT id FROM files WHERE path=? LIMIT 1", (path,)).fetchone()
        if not target and name:
            # 文件名可能重名（同名文件散落在不同目录），只有唯一命中才认，
            # 否则宁可跳过也别把标签糊到别的记录上。
            if isinstance(size, int) and size >= 0:
                hits = cur.execute(
                    "SELECT id FROM files WHERE name=? AND size=? LIMIT 2",
                    (name, size)).fetchall()
            else:
                hits = cur.execute(
                    "SELECT id FROM files WHERE name=? LIMIT 2", (name,)).fetchall()
            if not hits:
                hits = cur.execute(
                    "SELECT id FROM files WHERE name LIKE ? ESCAPE '!' LIMIT 2",
                    (f"%{like_escape(name)}%",)).fetchall()
            if len(hits) == 1:
                target = hits[0]
        if not target:
            skipped += 1
            continue
        cur.execute("UPDATE files SET tags=?, notes=? WHERE id=?",
                    (tags, notes, target[0]))
        ok += max(cur.rowcount, 0)
    return ok, skipped


# ==================== 主题 ====================
def detect_system_dark():
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return val == 0
    except Exception:
        return False


def apply_theme(root, dark=False):
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    if dark:
        bg, fg, field = "#1e1e1e", "#dcdcdc", "#2b2b2b"
        sel, head, line = "#094771", "#2d2d30", "#3c3c3c"
    else:
        bg, fg, field = "#f5f5f5", "#000000", "#ffffff"
        sel, head, line = "#cce8ff", "#e8e8e8", "#cccccc"
    root.configure(bg=bg)
    style.configure(".", background=bg, foreground=fg, fieldbackground=field)
    style.configure("TFrame", background=bg)
    style.configure("TLabel", background=bg, foreground=fg)
    style.configure("TButton", background=head, foreground=fg)
    style.map("TButton", background=[("active", sel)])
    # 统一按钮风格：padding 缩小到 (5,1)
    style.configure("App.TButton", padding=(5, 1),
                    font=("Microsoft YaHei", 9),
                    background=head, foreground=fg)
    style.map("App.TButton",
              background=[("active", sel), ("pressed", sel)])
    style.configure("TEntry", fieldbackground=field, foreground=fg, insertcolor=fg)
    style.configure("TCheckbutton", background=bg, foreground=fg)
    style.configure("TRadiobutton", background=bg, foreground=fg)
    style.configure("TCombobox", fieldbackground=field, foreground=fg,
                    background=head, arrowcolor=fg, insertcolor=fg,
                    selectbackground=field, selectforeground=fg)
    # readonly 状态（"MB"/"500" 这类下拉框）默认用主题自带的灰底灰字，
    # 暗色主题下会变成浅底浅字、完全看不清，必须显式给 readonly 的配色
    style.map("TCombobox",
              fieldbackground=[("readonly", field), ("disabled", bg)],
              foreground=[("readonly", fg), ("disabled", "#8a8a8a")],
              background=[("readonly", head), ("active", sel)],
              selectbackground=[("readonly", field)],
              selectforeground=[("readonly", fg)],
              arrowcolor=[("disabled", "#8a8a8a"), ("active", fg)])
    # 下拉列表是普通 Tk listbox，不吃 ttk.Style，只能用 option 数据库设色
    try:
        root.option_add("*TCombobox*Listbox.background", field)
        root.option_add("*TCombobox*Listbox.foreground", fg)
        root.option_add("*TCombobox*Listbox.selectBackground", sel)
        root.option_add("*TCombobox*Listbox.selectForeground", fg)
    except Exception:
        pass
    style.configure("TNotebook", background=bg, borderwidth=0)
    style.configure("TNotebook.Tab", background=head, foreground=fg, padding=(10, 4))
    style.map("TNotebook.Tab", background=[("selected", bg)])
    style.configure("Treeview", background=field, foreground=fg,
                    fieldbackground=field, rowheight=26, borderwidth=0)
    style.configure("Treeview.Heading", background=head, foreground=fg, relief="flat")
    style.map("Treeview", background=[("selected", sel)], foreground=[("selected", fg)])
    style.map("Treeview.Heading", background=[("active", sel)])
    style.configure("TScrollbar", background=head, troughcolor=bg, arrowcolor=fg)
    style.configure("TSeparator", background=line)
    style.configure("TPanedwindow", background=bg)
    style.configure("Sash", background=line, sashthickness=6)
    return {"bg": bg, "fg": fg, "field": field, "head": head, "line": line}


# ==================== GUI ====================
def run_gui():
    root = TkinterDnD.Tk()
    root.title(f"{APP_NAME} {APP_VERSION}")
    root.geometry("1500x820")
    root.minsize(1000, 600)

    # 设置窗口图标（标题栏 + 任务栏）
    try:
        ico = resource_path("app.ico")
        if os.path.exists(ico):
            root.iconbitmap(ico)
    except Exception:
        pass

    installed_raw = []
    installed_norm = set()
    theme_colors = {"current": None}
    dark_var = tk.BooleanVar(value=detect_system_dark())

    B = "App.TButton"

    # ---------- 第一行 ----------
    row1 = ttk.Frame(root, padding=(8, 8, 8, 2)); row1.pack(fill="x")
    ttk.Label(row1, text="扫描目录:").pack(side="left")
    dir_var = tk.StringVar(value="")
    ttk.Entry(row1, textvariable=dir_var, width=26).pack(side="left", padx=4)

    def add_dir():
        d = filedialog.askdirectory()
        if d:
            cur = dir_var.get().strip()
            dir_var.set((cur + ";" if cur else "") + d)

    ttk.Button(row1, text="浏览...", style=B, command=add_dir).pack(side="left", padx=2)
    btn_scan = ttk.Button(row1, text="▶ 开始扫描", style=B); btn_scan.pack(side="left", padx=2)
    btn_imp = ttk.Button(row1, text="📥 导入标签", style=B); btn_imp.pack(side="left", padx=2)
    btn_exp = ttk.Button(row1, text="📤 导出标签", style=B); btn_exp.pack(side="left", padx=2)
    btn_export_sel = ttk.Button(row1, text="📋 导出选中详情", style=B); btn_export_sel.pack(side="left", padx=2)
    btn_all = ttk.Button(row1, text="📦 导出全部详情", style=B); btn_all.pack(side="left", padx=2)
    btn_sw_export = ttk.Button(row1, text="🧩 导出软件详情", style=B); btn_sw_export.pack(side="left", padx=2)

    row1_right = ttk.Frame(row1); row1_right.pack(side="right", padx=2)

    # ---------- 第二行 ----------
    row2 = ttk.Frame(root, padding=(8, 2, 8, 4)); row2.pack(fill="x")
    ttk.Label(row2, text="搜索名称:").pack(side="left")
    search_var = tk.StringVar()
    search_entry = ttk.Entry(row2, textvariable=search_var, width=26)
    search_entry.pack(side="left", padx=(4, 5))
    stat_var = tk.StringVar(value="")
    ttk.Label(row2, textvariable=stat_var, foreground="gray").pack(side="left", padx=(0, 8))
    ttk.Label(row2, text="路径范围:").pack(side="left")
    path_filter_var = tk.StringVar()
    path_entry = ttk.Entry(row2, textvariable=path_filter_var, width=13)
    path_entry.pack(side="left", padx=(4, 8))
    ttk.Label(row2, text="限制大小:").pack(side="left")
    size_min_var = tk.StringVar()
    ttk.Entry(row2, textvariable=size_min_var, width=13).pack(side="left", padx=(4, 0))
    ttk.Label(row2, text="~").pack(side="left", padx=2)
    size_max_var = tk.StringVar()
    ttk.Entry(row2, textvariable=size_max_var, width=13).pack(side="left")
    size_unit_var = tk.StringVar(value="MB")
    size_unit_cb = ttk.Combobox(row2, textvariable=size_unit_var, width=4,
                                state="readonly", values=["MB", "GB"])
    size_unit_cb.pack(side="left", padx=(4, 8))
    ttk.Label(row2, text="限制时间:").pack(side="left")
    time_min_var = tk.StringVar()
    ttk.Entry(row2, textvariable=time_min_var, width=13).pack(side="left", padx=(4, 0))
    ttk.Label(row2, text="~").pack(side="left", padx=2)
    time_max_var = tk.StringVar()
    ttk.Entry(row2, textvariable=time_max_var, width=13).pack(side="left", padx=(0, 8))

    def reset_filters():
        search_var.set(""); path_filter_var.set("")
        size_min_var.set(""); size_max_var.set(""); size_unit_var.set("MB")
        time_min_var.set(""); time_max_var.set("")
        PAGE_INDEX_VAR.set(0)
        _sw_cache_key["k"] = None
        refresh()

    ttk.Button(row2, text="🔄 重置", style=B, command=reset_filters).pack(side="left", padx=(8, 4))

    # ---------- Notebook ----------
    notebook = ttk.Notebook(root); notebook.pack(fill="both", expand=True, padx=8, pady=4)

    # ===== Tab1 文件视图 =====
    tab_file = ttk.Frame(notebook); notebook.add(tab_file, text="📄 文件视图")
    tab_file.rowconfigure(1, weight=1)
    tab_file.columnconfigure(0, weight=1)

    file_toolbar = ttk.Frame(tab_file, padding=(4, 4, 4, 2))
    file_toolbar.grid(row=0, column=0, sticky="ew")

    paned = ttk.PanedWindow(tab_file, orient="horizontal")
    paned.grid(row=1, column=0, sticky="nsew")

    left_frame = ttk.Frame(paned)
    paned.add(left_frame, weight=1)

    # 侧边栏固定初始宽度
    SIDE_WIDTH = {"value": 360}
    side_panel = ttk.Frame(paned, width=SIDE_WIDTH["value"])
    # 默认关闭，这里不 add，由 _auto_fullscreen_side / toggle_side 决定

    # 侧边栏状态提前定义，_init_sash / _on_sash_drag 都要用
    side_visible = {"on": False, "manual": False, "user_sash": False}

    _sash_guard = {"busy": False}

    def _init_sash(force=False):
        if _sash_guard["busy"]:
            return
        if not force and side_visible.get("user_sash", False):
            return
        # 新增：少于 2 个 pane 时没有 sash，直接返回，避免 sashpos 异常干扰布局
        try:
            if len(paned.panes()) < 2:
                return
        except Exception:
            return
        _sash_guard["busy"] = True
        try:
            paned.update_idletasks()
            total = paned.winfo_width()

            if total <= 1:
                return
            target_side = SIDE_WIDTH["value"]
            if total < target_side + 240:
                target_side = max(180, int(total * 0.4))
            sash = max(150, total - target_side)
            cur = paned.sashpos(0)
            if cur != sash:
                paned.sashpos(0, sash)
        except Exception:
            pass
        finally:
            root.after(30, lambda: _sash_guard.__setitem__("busy", False))

    def _on_sash_drag(e=None):
        try:
            if side_visible["on"]:
                side_visible["user_sash"] = True
        except Exception:
            pass
    paned.bind("<ButtonRelease-1>", _on_sash_drag, add="+")

    root.after(80, _init_sash)
    root.after(300, _init_sash)

    def _is_fullscreen_like():
        try:
            if root.state() == "zoomed":
                return True
            sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
            w, h = root.winfo_width(), root.winfo_height()
            return w >= sw * 0.98 and h >= sh * 0.95
        except Exception:
            return False

    def _open_side():
        """打开侧边栏（供 _auto_fullscreen_side / toggle_side 复用）。"""
        if str(side_panel) not in paned.panes():
            paned.add(side_panel, weight=0)
        side_panel.configure(width=SIDE_WIDTH["value"])
        side_visible["on"] = True
        root.after(80, lambda: _init_sash(force=True))
        root.after(200, lambda: _init_sash(force=True))

    def _auto_fullscreen_side():
        if side_visible["manual"]:
            return
        if _is_fullscreen_like() and not side_visible["on"]:
            side_visible["user_sash"] = False  # 重新打开时重置，让宽度回到设定值
            _open_side()

    _cfg_guard = {"pending": False}

    def _on_root_configure(e=None):
        if e is not None and e.widget is not root:
            return
        if _cfg_guard["pending"]:
            return
        _cfg_guard["pending"] = True

        def _do():
            _cfg_guard["pending"] = False
            # 只在“侧边栏已打开”且“窗口尺寸变了”时校正一次
            # 但用户手动拖过 sash 后不再强制覆盖
            if side_visible["on"] and not side_visible.get("user_sash", False):
                _init_sash()
            _auto_fullscreen_side()

        root.after(60, _do)

    root.after(120, _auto_fullscreen_side)
    root.after(400, _auto_fullscreen_side)
    root.bind("<Configure>", _on_root_configure)


    side_title = tk.Label(side_panel, text="未选中文件",
                          font=("Microsoft YaHei", 10, "bold"), anchor="w")
    side_title.pack(anchor="w", fill="x", padx=8, pady=6)
    side_text_wrap = ttk.Frame(side_panel)
    side_text_wrap.pack(fill="both", expand=True, padx=8, pady=(0, 8))
    side_text = tk.Text(side_text_wrap, wrap="word", width=1, height=1,
                        font=("Microsoft YaHei", 9), relief="flat",
                        borderwidth=0)
    # 彻底移除 side_vsb，让文本框占满整个侧边栏
    side_text.pack(fill="both", expand=True)

    def apply_side_colors():
        c = theme_colors["current"]
        if not c:
            return
        side_title.configure(fg=c["fg"], bg=c["bg"])
        side_text.configure(bg=c["field"], fg=c["fg"], insertbackground=c["fg"])

    def toggle_side():
        side_visible["manual"] = True
        if side_visible["on"]:
            try:
                paned.forget(side_panel)
            except Exception:
                pass
            side_visible["on"] = False
        else:
            _open_side()

    btn_side = ttk.Button(file_toolbar, text="📄 详情栏",
                          style=B, command=toggle_side)
    btn_side.pack(side="left", padx=4)

    # --- 分页栏先 pack 到底部（必须在 list_view 之前 pack，否则会被 list_view 吃掉空间）---
    PAGE_SIZE_VAR = tk.IntVar(value=500)
    PAGE_INDEX_VAR = tk.IntVar(value=0)
    TOTAL_ROWS_VAR = tk.IntVar(value=0)
    PAGE_INFO_VAR = tk.StringVar(value="")
    page_bar = ttk.Frame(left_frame)
    page_bar.pack(side="bottom", fill="x", pady=(4, 0))

    # --- 列表 ---
    list_view = ttk.Frame(left_frame)
    list_view.pack(fill="both", expand=True)

    cols = ("name", "version", "desc", "tags", "notes", "installed_status", "path")
    heads = {
        "name": ("文件名", 320), "version": ("版本", 100), "desc": ("内部描述", 320),
        "tags": ("标签", 120), "notes": ("备注", 180),
        "installed_status": ("安装状态", 100), "path": ("所在目录", 260),
    }
    COLUMN_STATE = {c: {"visible": True, "width": w} for c, (_, w) in heads.items()}
    tree = ttk.Treeview(list_view, columns=cols, show="headings", selectmode="extended")

    for c in cols:
        t, w = heads[c]
        tree.heading(c, text=t, command=lambda c=c: sort_by(c))
        min_w = max(50, int(w * 0.6))
        tree.column(c, width=w, minwidth=min_w, anchor="center", stretch=True)

    list_view.rowconfigure(0, weight=1)
    list_view.columnconfigure(0, weight=1)
    list_view.columnconfigure(1, weight=0, minsize=16)   # ← vsb 列固定 16px，不被压 0

    vsb = ttk.Scrollbar(list_view, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=vsb.set)

    tree.grid(row=0, column=0, sticky="nsew")
    vsb.grid(row=0, column=1, sticky="ns")

    # ★ 新增：容器尺寸变化时主动刷新 yscrollcommand，修复滚动条失效
    def _on_tree_configure(event=None):
        try:
            tree.yview_scroll(0, "units")
        except Exception:
            pass
    tree.bind("<Configure>", _on_tree_configure, add="+")

    row_map = {}
    sort_state = {"col": "mtime", "desc": True}

    # ===== 拖拽框选 =====
    drag = {"start": None, "append": False, "on_heading": False}

    def on_drag_press(e):
        region = tree.identify_region(e.x, e.y)
        if region in ("heading", "separator"):
            drag["on_heading"] = True
            drag["start"] = None
            return
        drag["on_heading"] = False
        drag["start"] = (e.x, e.y)
        drag["append"] = bool(e.state & 0x4)

    def on_drag_motion(e):
        if drag["start"] is None or drag["on_heading"]:
            return
        x0, y0 = drag["start"]
        if abs(e.x - x0) < 4 and abs(e.y - y0) < 4:
            return
        _select_in_rect(y0, e.y, live=True)

    def on_drag_release(e):
        if drag["start"] is not None and not drag["on_heading"]:
            x0, y0 = drag["start"]
            if abs(e.x - x0) >= 4 or abs(e.y - y0) >= 4:
                _select_in_rect(y0, e.y, live=False)
        drag["start"] = None
        drag["on_heading"] = False

    def _select_in_rect(y0, y1, live=False):
        top, bot = min(y0, y1), max(y0, y1)
        items = tree.get_children()
        if not items:
            return
        sel = set(tree.selection()) if (drag["append"] and live) else set()
        for iid in items:
            bbox = tree.bbox(iid)
            if not bbox:
                continue
            bx, by, bw, bh = bbox
            if by + bh >= top and by <= bot:
                sel.add(iid)
        tree.selection_set(list(sel))

    tree.bind("<Button-1>", on_drag_press, add="+")
    tree.bind("<B1-Motion>", on_drag_motion, add="+")
    tree.bind("<ButtonRelease-1>", on_drag_release, add="+")

    def go_page(p):
        PAGE_INDEX_VAR.set(max(0, p)); refresh()

    # Tkinter 居中的常用做法：左右各放一个自动拉伸的空白 Frame 占位
    ttk.Frame(page_bar).pack(side="left", expand=True)

    ttk.Button(page_bar, text="⏮", style=B, command=lambda: go_page(0)).pack(side="left", padx=2)
    ttk.Button(page_bar, text="◀", style=B, command=lambda: go_page(PAGE_INDEX_VAR.get() - 1)).pack(side="left", padx=2)
    ttk.Label(page_bar, textvariable=PAGE_INFO_VAR).pack(side="left", padx=8)
    ttk.Button(page_bar, text="▶", style=B, command=lambda: go_page(PAGE_INDEX_VAR.get() + 1)).pack(side="left", padx=2)
    ttk.Button(page_bar, text="⏭", style=B, command=lambda: go_page(10 ** 9)).pack(side="left", padx=2)
    ttk.Label(page_bar, text="每页:").pack(side="left", padx=(12, 2))
    ps_cb = ttk.Combobox(page_bar, textvariable=PAGE_SIZE_VAR,
                         values=[200, 500, 1000, 2000], width=6, state="readonly")
    ps_cb.pack(side="left", padx=2)

    ttk.Frame(page_bar).pack(side="left", expand=True)

    def update_page_bar():
        ps = PAGE_SIZE_VAR.get(); total = TOTAL_ROWS_VAR.get(); page = PAGE_INDEX_VAR.get()
        maxp = max(0, (total - 1) // ps) if total else 0
        PAGE_INFO_VAR.set(f"第 {page + 1} / {maxp + 1} 页  共 {total} 条")

    # ===== Tab2 软件视图 =====
    tab_sw = ttk.Frame(notebook); notebook.add(tab_sw, text="🧩 软件视图")
    sw_filter_bar = ttk.Frame(tab_sw, padding=(4, 4, 4, 2)); sw_filter_bar.pack(fill="x")
    sw_multi_only_var = tk.BooleanVar(value=False)
    btn_multi_only = ttk.Button(sw_filter_bar, text="🔀 多版本", style=B)
    btn_multi_only.pack(side="left")

    def toggle_multi_only():
        sw_multi_only_var.set(not sw_multi_only_var.get())
        btn_multi_only.config(
            text="🔀 多版本 ✓" if sw_multi_only_var.get() else "🔀 多版本")
        refresh_software_view(force=True)

    btn_multi_only.config(command=toggle_multi_only)

    sw_stat_var = tk.StringVar(value="")
    ttk.Label(sw_filter_bar, textvariable=sw_stat_var, foreground="gray").pack(side="right")

    sw_body = ttk.Frame(tab_sw); sw_body.pack(fill="both", expand=True)
    sw_cols = ("display", "latest_ver", "ver_count", "size_total", "size_latest",
               "date_range", "types", "tags", "status")
    sw_heads = {
        "display": ("软件名", 260), "latest_ver": ("最新版", 120), "ver_count": ("版本数", 70),
        "size_total": ("总大小", 100), "size_latest": ("最新版大小", 110),
        "date_range": ("日期范围", 220), "types": ("安装包类型", 170),
        "tags": ("标签", 160), "status": ("安装状态", 100),
    }
    sw_sort_state = {"col": None, "desc": True}

    def sort_sw_by(c):
        if sw_sort_state["col"] == c:
            sw_sort_state["desc"] = not sw_sort_state["desc"]
        else:
            sw_sort_state["col"], sw_sort_state["desc"] = c, True
        refresh_software_view(force=True)

    sw_body.rowconfigure(0, weight=1)
    sw_body.columnconfigure(0, weight=1)
    sw_body.columnconfigure(1, weight=0, minsize=16)

    sw_tree = ttk.Treeview(sw_body, columns=sw_cols, show="headings", selectmode="extended")
    for c in sw_cols:
        t, w = sw_heads[c]
        sw_tree.heading(c, text=t, command=lambda c=c: sort_sw_by(c))
        # minwidth 取列默认宽的一部分，避免窄窗口压成一条线
        min_w = max(40, int(w * 0.6))
        sw_tree.column(c, width=w, minwidth=min_w, anchor="center", stretch=True)

    sw_vsb = ttk.Scrollbar(sw_body, orient="vertical", command=sw_tree.yview)
    sw_tree.configure(yscrollcommand=sw_vsb.set)

    sw_tree.grid(row=0, column=0, sticky="nsew")
    sw_vsb.grid(row=0, column=1, sticky="ns")

    # ★ 新增：同上，防止软件视图滚动条也失效
    def _on_sw_tree_configure(event=None):
        try:
            sw_tree.yview_scroll(0, "units")
        except Exception:
            pass
    sw_tree.bind("<Configure>", _on_sw_tree_configure, add="+")
    sw_row_map = {}
    sw_groups_cache = []
    sw_virtual_loaded = tk.IntVar(value=0)
    SW_VIRTUAL_STEP = 200
    _sw_cache_key = {"k": None}

    def _sw_filter_key():
        return (search_var.get(), size_min_var.get(), size_max_var.get(),
                size_unit_var.get(), time_min_var.get(), time_max_var.get(),
                path_filter_var.get(), sw_multi_only_var.get(),
                sw_sort_state["col"], sw_sort_state["desc"])

    status_var = tk.StringVar(value="软件启动中，正在后台检测本机已安装软件...")
    ttk.Label(root, textvariable=status_var, relief="sunken",
              anchor="w", padding=4).pack(fill="x")

    def open_column_settings():
        win = tk.Toplevel(root); win.title("宽度设置"); win.geometry("380x520")
        win.transient(root)
        frm = ttk.Frame(win, padding=10); frm.pack(fill="both", expand=True)
        rows_ui = {}
        for idx, (c, (title, _)) in enumerate(heads.items()):
            st = COLUMN_STATE[c]
            var_show = tk.BooleanVar(value=st["visible"])
            ttk.Checkbutton(frm, text=title, variable=var_show).grid(row=idx, column=0, sticky="w", pady=2)
            var_w = tk.StringVar(value=str(st["width"]))
            ttk.Entry(frm, textvariable=var_w, width=6).grid(row=idx, column=1, padx=4)
            rows_ui[c] = (var_show, var_w)

        r = len(heads)
        ttk.Separator(frm, orient="horizontal").grid(
            row=r, column=0, columnspan=2, sticky="ew", pady=(8, 4))
        ttk.Label(frm, text="详情栏宽度").grid(row=r + 1, column=0, sticky="w", pady=2)
        side_w_var = tk.StringVar(value=str(SIDE_WIDTH["value"]))
        ttk.Entry(frm, textvariable=side_w_var, width=6).grid(row=r + 1, column=1, padx=4)

        def apply():
            for c, (var_show, var_w) in rows_ui.items():
                COLUMN_STATE[c]["visible"] = var_show.get()
                try:
                    w = int(var_w.get())
                except Exception:
                    w = heads[c][1]
                COLUMN_STATE[c]["width"] = w
                tree.column(c, width=w)
            visible_cols = [c for c in cols if COLUMN_STATE[c]["visible"]]
            if not visible_cols:
                # 空 displaycolumns 在不同 Tk 版本下含义不一致，显式回落成全部显示
                visible_cols = list(cols)
            tree.configure(displaycolumns=visible_cols)
            try:
                sw = int(side_w_var.get())
                SIDE_WIDTH["value"] = max(200, min(800, sw))
            except Exception:
                pass
            side_panel.configure(width=SIDE_WIDTH["value"])
            if side_visible["on"]:
                side_visible["user_sash"] = False
                root.after(50, lambda: _init_sash(force=True))
            win.destroy()
        ttk.Button(win, text="应用", style=B, command=apply).pack(pady=6)
    ttk.Button(file_toolbar, text="⚙ 宽度设置", style=B,
               command=open_column_settings).pack(side="left", padx=4)

    # 新增：让 paned / list_view / vsb 按最终高度重新布局，并触发 yscrollcommand
    def _force_layout_fix():
        try:
            tab_file.update_idletasks()
            paned.update_idletasks()
            left_frame.update_idletasks()
            list_view.update_idletasks()
            tree.update_idletasks()
            tree.yview_scroll(0, "units")
        except Exception:
            pass
    root.after(60, _force_layout_fix)
    root.after(300, _force_layout_fix)
    root.after(1000, _force_layout_fix)


    def toggle_dark():
        dark_var.set(not dark_var.get())
        theme_colors["current"] = apply_theme(root, dark_var.get())
        apply_side_colors()
        btn_dark.config(text="☀ 亮色" if dark_var.get() else "🌙 暗色")

    btn_dark = ttk.Button(row1_right, text="🌙 暗色" if not dark_var.get() else "☀ 亮色",
                          style=B, command=toggle_dark)
    btn_dark.pack(side="left", padx=4)

    # ===== 侧边栏刷新 =====
    def update_side_panel_by_iid(iid):
        d = row_map.get(iid)
        side_text.delete("1.0", "end")
        if not d:
            side_title.config(text="未选中文件"); return
        side_title.config(text=clean_text(d["name"]))
        info = [
            f"【版本】{d.get('version', '') or '—'}",
            f"【描述】{d.get('full_desc', '') or '—'}",
            f"【时间】{d.get('mtime', '') or '—'}",
            f"【标签】{d.get('tags', '') or '—'}",
            f"【备注】{d.get('notes', '') or '—'}",
            f"【路径】\n{d.get('path', '')}",
        ]
        side_text.insert("1.0", "\n\n".join(info))

    def update_side_panel(event=None):
        sel = tree.selection()
        if not sel:
            update_side_panel_by_iid(None); return
        update_side_panel_by_iid(sel[0])

    tree.bind("<<TreeviewSelect>>", update_side_panel)

    # ===== 选中 =====
    def get_current_selected_ids():
        return list(tree.selection())

    def export_selected():
        sel = get_current_selected_ids()
        if not sel:
            messagebox.showwarning("提示", "请先选中要导出的文件！"); return
        ordered_ids = [iid for iid in tree.get_children() if iid in set(sel)]
        if not ordered_ids:
            return
        filepath = filedialog.asksaveasfilename(title="导出选中详情", defaultextension=".csv",
                                                filetypes=[("CSV 表格文件", "*.csv")],
                                                initialfile=f"选中详情_{len(ordered_ids)}条.csv")
        if not filepath:
            return
        try:
            con = db_connect(); cur = con.cursor()
            ph = ",".join("?" * len(ordered_ids))
            cur.execute(f"""SELECT id,name,ext,size,version,product,description,inner,mtime,path,notes,tags
                            FROM files WHERE id IN ({ph})""", ordered_ids)
            raw = {str(r[0]): r for r in cur.fetchall()}; con.close()
            with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["文件名", "后缀", "大小(Bytes)", "版本", "产品名", "描述",
                            "压缩包内容", "修改时间", "完整路径", "标签", "备注"])
                for iid in ordered_ids:
                    r = raw.get(iid)
                    if not r:
                        continue
                    out = list(r[1:])
                    if out[7]:
                        out[7] = fmt_time(out[7])
                    w.writerow(out)
            messagebox.showinfo("成功", f"成功导出 {len(ordered_ids)} 条选中记录！\n\n已保存至:\n{filepath}")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))
    btn_export_sel.config(command=export_selected)

    def export_all_csv():
        filepath = filedialog.asksaveasfilename(title="导出全部扫描数据", defaultextension=".csv",
                                                filetypes=[("CSV 表格文件", "*.csv")],
                                                initialfile="全部安装包数据.csv")
        if not filepath:
            return
        try:
            con = db_connect(); cur = con.cursor()
            cur.execute("SELECT name, ext, size, version, product, description, inner, "
                        "mtime, path, notes, tags FROM files ORDER BY name")
            rows = cur.fetchall(); con.close()
            with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["文件名", "后缀", "大小(Bytes)", "版本", "产品名", "描述",
                                 "压缩包内容", "修改时间戳", "完整路径", "标签", "备注"])
                for r in rows:
                    if r[7]:
                        r = list(r); r[7] = fmt_time(r[7])
                    writer.writerow(r)
            messagebox.showinfo("成功", f"成功导出了 {len(rows)} 条完整数据！\n\n已保存至:\n{filepath}")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))
    btn_all.config(command=export_all_csv)

    # ===== 拖拽导入 =====
    def drop_files(event):
        files = root.tk.splitlist(event.data)
        valid = [f for f in files if os.path.isfile(f) and os.path.splitext(f)[1].lower() in PACK_EXT]
        if not valid:
            messagebox.showwarning("提示", "没有识别到支持的安装包格式！"); return
        con = db_connect(); cur = con.cursor(); cur.executescript(SCHEMA)
        added_paths = [p for p in valid if upsert_file(cur, p)]
        con.commit()
        if added_paths:
            new_tags = simpledialog.askstring("快速打标签",
                                              f"成功导入 {len(added_paths)} 个文件！\n给它们加个标签吧：")
            if new_tags and new_tags.strip():
                con.executemany("UPDATE files SET tags=? WHERE path=?",
                                [(new_tags.strip(), p) for p in added_paths])
                con.commit()
        con.close()
        _sw_cache_key["k"] = None
        refresh()
        if not added_paths:
            status_var.set("拖入的文件都已经在库里了，无需重复添加。")
    tree.drop_target_register(DND_FILES); tree.dnd_bind('<<Drop>>', drop_files)

    # ===== 详情弹窗（自适应，宽度 42%）=====
    def show_details():
        sel = tree.selection()
        if not sel or sel[0] not in row_map:
            return
        iid = sel[0]
        data = row_map[iid]
        win = tk.Toplevel(root); win.title(f"文件详情 - {data['name']}")
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w = 480
        h = int(sh * 0.65)
        win.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")
        win.minsize(440, 380)
        win.resizable(True, True)
        win.transient(root)
        c = theme_colors["current"]
        if c:
            win.configure(bg=c["bg"])
        tk.Label(win, text="📄 文件详细信息",
                 font=("Microsoft YaHei", 12, "bold")).pack(pady=5)
        info_frame = ttk.Frame(win, padding=10); info_frame.pack(fill="x")
        for label, value in [("【文件名】", data['name']), ("【版本号】", data['version'] or "未知"),
                             ("【内部描述】", data['full_desc']), ("【修改时间】", data['mtime']),
                             ("【完整路径】", data['path'])]:
            tk.Label(info_frame, text=label,
                     font=("Microsoft YaHei", 10, "bold")).pack(anchor="w")
            tk.Label(info_frame, text=value, font=("Microsoft YaHei", 9), fg="#777",
                     wraplength=w - 80, justify="left").pack(anchor="w", pady=(0, 5))
        ttk.Separator(win, orient='horizontal').pack(fill='x', padx=10, pady=5)
        edit_frame = ttk.Frame(win, padding=10); edit_frame.pack(fill="x")
        ttk.Label(edit_frame, text="标签:").grid(row=0, column=0, sticky="w", pady=2)
        tags_var = tk.StringVar(value=data["tags"])
        ttk.Entry(edit_frame, textvariable=tags_var, width=50).grid(row=0, column=1, sticky="w", padx=5, pady=2)
        ttk.Label(edit_frame, text="备注:").grid(row=1, column=0, sticky="w", pady=2)
        notes_var = tk.StringVar(value=data["notes"])
        ttk.Entry(edit_frame, textvariable=notes_var, width=50).grid(row=1, column=1, sticky="w", padx=5, pady=2)

        def save_changes():
            new_tags = tags_var.get().strip(); new_notes = notes_var.get().strip()
            con = db_connect()
            con.execute("UPDATE files SET tags=?, notes=? WHERE id=?",
                        (new_tags, new_notes, iid))
            con.commit(); con.close()
            row_map[iid]["tags"] = new_tags
            row_map[iid]["notes"] = new_notes
            messagebox.showinfo("成功", "标签和备注已更新！", parent=win)
            win.destroy()
            _sw_cache_key["k"] = None
            refresh()
        btnf = ttk.Frame(win, padding=10); btnf.pack(fill="x", side="bottom")
        ttk.Button(btnf, text="💾 保存修改", style=B, command=save_changes).pack(side="left", padx=5)
        ttk.Button(btnf, text="关闭", style=B, command=win.destroy).pack(side="right")

    # ===== 导出/导入标签 =====
    def export_data():
        con = db_connect(); cur = con.cursor()
        data = export_tags_rows(cur)
        con.close()
        if not data:
            messagebox.showinfo("提示", "当前没有设置任何标签或备注，无需导出。"); return
        fp = filedialog.asksaveasfilename(title="导出标签与备注", defaultextension=".json",
                                          filetypes=[("JSON 文件", "*.json")],
                                          initialfile="PackManager_Backup.json")
        if not fp:
            return
        try:
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
            messagebox.showinfo("成功", f"成功导出了 {len(data)} 条标签/备注数据！\n\n文件已保存至:\n{fp}")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))

    def import_data():
        fp = filedialog.askopenfilename(title="导入标签与备注",
                                        filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")])
        if not fp:
            return
        try:
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            messagebox.showerror("读取失败", f"无法读取文件。\n{e}"); return
        if not isinstance(data, list):
            messagebox.showerror("格式不对", "备份文件应该是一个 JSON 数组。"); return
        con = db_connect(); cur = con.cursor()
        ok, skipped = restore_tags(cur, data)
        con.commit(); con.close()
        if ok > 0:
            extra = f"\n（{skipped} 条在库中找不到唯一对应文件，已跳过）" if skipped else ""
            messagebox.showinfo("导入成功", f"成功匹配并恢复了 {ok} 条标签/备注数据！{extra}")
            _sw_cache_key["k"] = None
            refresh()
        else:
            messagebox.showwarning("未匹配到文件", "导入的数据没有在库中找到。")
    btn_exp.config(command=export_data); btn_imp.config(command=import_data)

    # ===== 刷新文件视图 =====
    def refresh(*_):
        ps = PAGE_SIZE_VAR.get()
        total, page, rows = fetch_page(
            search_var.get(), sort_state["col"], sort_state["desc"],
            page=PAGE_INDEX_VAR.get(), page_size=ps,
            size_min=size_min_var.get(), size_max=size_max_var.get(),
            size_unit=size_unit_var.get(),
            time_min=time_min_var.get(), time_max=time_max_var.get(),
            path_filter=path_filter_var.get())
        TOTAL_ROWS_VAR.set(total)
        PAGE_INDEX_VAR.set(page)
        tree.delete(*tree.get_children()); row_map.clear()
        for r in rows:
            _id, name, ext, size, version, product, description, mtime, path, notes, tags = r
            safe_name = clean_text(name) or "未知"
            safe_ver = clean_text(version) or " "
            safe_tags = clean_text(tags) or " "
            safe_notes = clean_text(notes) or " "
            safe_mtime = fmt_time(mtime) if mtime is not None else ""
            safe_dir = clean_text(os.path.dirname(os.path.normpath(str(path))))
            cp, cd = clean_text(product), clean_text(description)
            parts = []
            for x in (cp, cd):
                if x and x not in parts:
                    parts.append(x)
            full_desc = " | ".join(parts) if parts else ""
            safe_desc = full_desc[:30]
            iid = str(_id)
            row_map[iid] = {"path": path, "name": safe_name, "notes": safe_notes.strip(),
                            "tags": safe_tags.strip(), "full_desc": full_desc,
                            "version": safe_ver, "mtime": safe_mtime}
            st = "✅ 已安装" if match_installed(safe_name, cp, installed_norm) else ""
            tree.insert("", "end", iid=iid,
                        values=[safe_name, safe_ver, safe_desc, safe_tags, safe_notes, st, safe_dir])
        stat_var.set(f"当前 {len(rows)} / {total} 条")
        update_page_bar()
        refresh_software_view()

    # ===== 软件视图 =====
    def append_software_rows():
        start = sw_virtual_loaded.get()
        end = min(len(sw_groups_cache), start + SW_VIRTUAL_STEP)
        for g in sw_groups_cache[start:end]:
            latest = g["latest"]
            latest_ver = clean_text(latest[4]) or "—"
            ver_count = len(g["versions"])
            size_total = sum(g["sizes"])
            size_latest = latest[3] or 0
            mts = [m for m in g["mtimes"] if m]
            date_range = f"{fmt_time(min(mts))} ~ {fmt_time(max(mts))}" if mts else "—"
            types = ",".join(sorted(g["types"])) or "未知"
            tags = ",".join(sorted(g["tags"])) or " "
            st = "✅ 已安装" if g.get("_installed") else ""
            iid = g["key"]; base = iid; idx = 1
            while iid in sw_row_map:
                iid = f"{base}#{idx}"; idx += 1
            sw_row_map[iid] = g
            sw_tree.insert("", "end", iid=iid, values=[
                g["display"], latest_ver, ver_count,
                fmt_size(size_total), fmt_size(size_latest),
                date_range, types, tags, st])
        sw_virtual_loaded.set(end)

    def _sw_sort_value(g, col):
        latest = g["latest"]
        if col == "display":     return (g["display"] or "").lower()
        if col == "latest_ver":  return parse_version(latest[4])
        if col == "ver_count":   return len(g["versions"])
        if col == "size_total":  return sum(g["sizes"])
        if col == "size_latest": return latest[3] or 0
        if col == "date_range":  return latest[7] or 0
        if col == "types":       return ",".join(sorted(g["types"]))
        if col == "tags":        return ",".join(sorted(g["tags"]))
        if col == "status":      return 1 if g.get("_installed") else 0
        return 0

    def refresh_software_view(force=False):
        k = _sw_filter_key()
        if not force and k == _sw_cache_key["k"]:
            return
        _sw_cache_key["k"] = k
        groups = query_software_groups(
            search_var.get(),
            size_min=size_min_var.get(), size_max=size_max_var.get(),
            size_unit=size_unit_var.get(),
            time_min=time_min_var.get(), time_max=time_max_var.get(),
            path_filter=path_filter_var.get(),
        )
        if sw_multi_only_var.get():
            groups = [g for g in groups if len(g["versions"]) > 1]
        for g in groups:
            latest = g["latest"]
            g["_installed"] = match_installed(
                clean_text(latest[1]), clean_text(latest[5]), installed_norm)
        col = sw_sort_state["col"]
        if col:
            groups.sort(key=lambda g: _sw_sort_value(g, col), reverse=sw_sort_state["desc"])
        sw_tree.delete(*sw_tree.get_children()); sw_row_map.clear()
        sw_groups_cache.clear(); sw_groups_cache.extend(groups)
        sw_virtual_loaded.set(0)
        append_software_rows()
        multi = sum(1 for g in groups if len(g["versions"]) > 1)
        sw_stat_var.set(f"软件: {len(groups)} | 多版本: {multi}")

    def _try_load_more():
        try:
            _, last = sw_tree.yview()
            if last >= 0.99 and sw_virtual_loaded.get() < len(sw_groups_cache):
                append_software_rows()
        except Exception:
            pass

    def on_sw_scroll(*args):
        sw_tree.yview(*args)
        _try_load_more()

    sw_vsb.config(command=on_sw_scroll)
    # 鼠标滚轮也走同一逻辑
    def _sw_mousewheel(e):
        sw_tree.yview_scroll(int(-1 * (e.delta / 120)), "units")
        _try_load_more()
        return "break"
    sw_tree.bind("<MouseWheel>", _sw_mousewheel)


    def show_software_versions(event=None):
        sel = sw_tree.selection()
        if not sel:
            return
        g = sw_row_map.get(sel[0])
        if not g:
            return
        win = tk.Toplevel(root); win.title(f"版本列表 - {g['display']}")
        win.geometry("1080x560"); win.transient(root)
        rx, ry = root.winfo_rootx(), root.winfo_rooty()
        win.geometry(f"+{rx + 100}+{ry + 50}")
        size_count = {}; ver_count_map = {}
        for vt, row in g["versions"]:
            s = row[3] or 0; size_count[s] = size_count.get(s, 0) + 1
            vstr = clean_text(row[4]) or "—"; ver_count_map[vstr] = ver_count_map.get(vstr, 0) + 1
        tk.Label(win, text=f"🧩 {g['display']}   共 {len(g['versions'])} 个版本",
                 font=("Microsoft YaHei", 11, "bold")).pack(pady=6)
        legend = ttk.Frame(win); legend.pack(fill="x", padx=10, pady=(0, 4))
        tk.Label(legend, text="  ⭐ 最新版  ", bg="#fff9c4").pack(side="left", padx=4)
        tk.Label(legend, text="  重复大小  ", bg="#ffe0b2").pack(side="left", padx=4)
        tk.Label(legend, text="  重复版本号  ", bg="#f8bbd0").pack(side="left", padx=4)
        tk.Label(legend, text="  旧版本(非最新)  ", bg="#e0e0e0").pack(side="left", padx=4)
        cols2 = ("ver", "name", "size", "type", "mtime", "path", "mark")
        heads2 = {"ver": ("版本", 110), "name": ("文件名", 300), "size": ("大小", 100),
                  "type": ("类型", 120), "mtime": ("修改时间", 140),
                  "path": ("路径", 460), "mark": ("标记", 90)}
        f2 = ttk.Frame(win); f2.pack(fill="both", expand=True, padx=8, pady=4)
        t2 = ttk.Treeview(f2, columns=cols2, show="headings")
        for c in cols2:
            t2.heading(c, text=heads2[c][0]); t2.column(c, width=heads2[c][1], anchor="center")
        v2 = ttk.Scrollbar(f2, orient="vertical", command=t2.yview)
        t2.configure(yscrollcommand=v2.set)
        t2.pack(side="left", fill="both", expand=True); v2.pack(side="right", fill="y")
        t2.tag_configure("latest", background="#fff9c4")
        t2.tag_configure("dup_size", background="#ffe0b2")
        t2.tag_configure("dup_ver", background="#f8bbd0")
        t2.tag_configure("old", foreground="#888888")
        latest_row = g["latest"]
        for vt, row in g["versions"]:
            (_id, name, ext, size, version, product, description, mtime, path,
             notes, tags, inner) = row
            is_latest = (row is latest_row)
            vstr = clean_text(version) or "—"; s = size or 0
            marks = []; tags_apply = []
            if is_latest:
                marks.append("⭐ 最新"); tags_apply.append("latest")
            if size_count.get(s, 0) > 1:
                tags_apply.append("dup_size"); marks.append("⚠同大小")
            if ver_count_map.get(vstr, 0) > 1:
                tags_apply.append("dup_ver"); marks.append("⚠同版本")
            if not is_latest:
                tags_apply.append("old")
            t2.insert("", "end", tags=tags_apply,
                      values=[vstr, clean_text(name), fmt_size(size),
                              detect_installer_type_cached(path, ext, inner, product,
                                                           description),
                              fmt_time(mtime), path, " ".join(marks)])

        def open_in_file_view():
            sel2 = t2.selection()
            if not sel2:
                return
            vals = t2.item(sel2[0], "values"); target = vals[5]
            notebook.select(0)
            path_filter_var.set(target); search_var.set("")
            _sw_cache_key["k"] = None
            refresh(); win.destroy()
        bf = ttk.Frame(win); bf.pack(fill="x", pady=6)
        ttk.Button(bf, text="📂 在文件视图中定位", style=B, command=open_in_file_view).pack(side="left", padx=8)
        ttk.Button(bf, text="关闭", style=B, command=win.destroy).pack(side="right", padx=8)

    def export_software_selected():
        sel = sw_tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请先在软件视图中选中要导出的软件！"); return
        ordered = [iid for iid in sw_tree.get_children() if iid in set(sel)]
        if not ordered:
            return
        fp = filedialog.asksaveasfilename(title="导出软件详情", defaultextension=".csv",
                                          filetypes=[("CSV 表格文件", "*.csv")],
                                          initialfile=f"软件详情_{len(ordered)}个软件.csv")
        if not fp:
            return
        try:
            with open(fp, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["软件名", "版本", "是否最新", "文件名", "后缀", "大小(Bytes)",
                            "修改时间", "安装包类型", "完整路径", "标签", "备注"])
                cnt = 0
                for iid in ordered:
                    g = sw_row_map.get(iid)
                    if not g:
                        continue
                    latest_row = g["latest"]
                    for vt, row in g["versions"]:
                        (_id, name, ext, size, version, product, description, mtime,
                         path, notes, tags, inner) = row
                        is_latest = (row is latest_row)
                        w.writerow([g["display"], clean_text(version) or "",
                                    "⭐ 最新" if is_latest else "",
                                    clean_text(name), ext, size or 0, fmt_time(mtime),
                                    detect_installer_type_cached(path, ext, inner, product,
                                                                 description),
                                    path, clean_text(tags), clean_text(notes)])
                        cnt += 1
            messagebox.showinfo("成功", f"成功导出 {len(ordered)} 个软件，共 {cnt} 条版本记录！\n\n已保存至:\n{fp}")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))
    btn_sw_export.config(command=export_software_selected)
    sw_tree.bind("<Double-1>", show_software_versions)
    sw_tree.bind("<Return>", show_software_versions)

    # ===== 排序 / 搜索绑定 =====
    def sort_by(c):
        if sort_state["col"] == c:
            sort_state["desc"] = not sort_state["desc"]
        else:
            sort_state["col"], sort_state["desc"] = c, False
        refresh()

    def trigger_search(event=None):
        _sw_cache_key["k"] = None
        refresh()

    for e in (search_entry, path_entry):
        e.bind("<Return>", trigger_search)
    size_unit_cb.bind("<<ComboboxSelected>>", trigger_search)
    ps_cb.bind("<<ComboboxSelected>>", lambda e: refresh())

    # ===== 右键菜单 =====
    def open_folder():
        sel = get_current_selected_ids()
        for iid in sel:
            p = row_map[iid]["path"]
            if not os.path.exists(p):
                continue
            try:
                if os.name == "nt":
                    subprocess.Popen(f'explorer /select,"{os.path.normpath(p)}"')
                else:
                    subprocess.Popen(["xdg-open", os.path.dirname(p)])
            except Exception:
                pass

    def run_file():
        sel = get_current_selected_ids()
        if not sel:
            return
        if len(sel) > 1:
            if not messagebox.askyesno("确认运行", f"你当前选中了 {len(sel)} 个文件！\n\n确定要同时运行吗？"):
                return
        for iid in sel[:5]:
            p = row_map[iid]["path"]
            if not os.path.exists(p):
                continue
            try:
                os.startfile(p)
            except Exception:
                pass

    def copy_path():
        sel = get_current_selected_ids()
        items = [row_map[i]["path"] for i in sel if i in row_map]
        if items:
            root.clipboard_clear(); root.clipboard_append("\n".join(items))

    def check_hash():
        sel = get_current_selected_ids()
        if not sel:
            return
        paths = [row_map[i]["path"] for i in sel if i in row_map]
        win = tk.Toplevel(root); win.title("校验哈希（SHA1）"); win.geometry("680x400")
        txt = tk.Text(win, wrap="none", font=("Consolas", 9))
        txt.pack(fill="both", expand=True, padx=6, pady=6)

        def worker():
            results = []
            for p in paths:
                try:
                    results.append(f"{sha1_file(p)}  {os.path.basename(p)}")
                except Exception as e:
                    results.append(f"ERROR  {os.path.basename(p)}  {e}")
            root.after(0, lambda: (txt.delete("1.0", "end"),
                                   txt.insert("1.0", "\n".join(results))))
        threading.Thread(target=worker, daemon=True).start()

        def copy_all():
            root.clipboard_clear()
            root.clipboard_append(txt.get("1.0", "end").strip())
            messagebox.showinfo("成功", "哈希已复制到剪贴板！", parent=win)
        bf = ttk.Frame(win); bf.pack(fill="x", pady=4)
        ttk.Button(bf, text="📋 复制全部", style=B, command=copy_all).pack(side="left", padx=6)
        ttk.Button(bf, text="关闭", style=B, command=win.destroy).pack(side="right", padx=6)

    def batch_edit_tags():
        sel = get_current_selected_ids()
        if not sel:
            return
        new_tags = simpledialog.askstring("批量编辑标签", f"选定了 {len(sel)} 个文件\n请输入标签：")
        if new_tags is None:
            return
        con = db_connect()
        con.executemany("UPDATE files SET tags=? WHERE id=?", [(new_tags.strip(), i) for i in sel])
        con.commit(); con.close()
        _sw_cache_key["k"] = None
        refresh()
        messagebox.showinfo("成功", f"已为 {len(sel)} 个文件设置标签！")

    def batch_edit_notes():
        sel = get_current_selected_ids()
        if not sel:
            return
        new_notes = simpledialog.askstring("批量编辑备注", f"选定了 {len(sel)} 个文件\n请输入备注：")
        if new_notes is None:
            return
        con = db_connect()
        con.executemany("UPDATE files SET notes=? WHERE id=?", [(new_notes.strip(), i) for i in sel])
        con.commit(); con.close()
        _sw_cache_key["k"] = None
        refresh()
        messagebox.showinfo("成功", f"已为 {len(sel)} 个文件添加备注！")

    def forget():
        sel = get_current_selected_ids()
        if not sel or not messagebox.askyesno("确认", f"从索引移除 {len(sel)} 条记录？\n(不会删除原文件)"):
            return
        con = db_connect()
        con.executemany("DELETE FROM files WHERE id=?", [(i,) for i in sel])
        con.commit(); con.close()
        _sw_cache_key["k"] = None
        refresh()

    def select_all():
        tree.selection_set(tree.get_children())

    def invert_selection():
        allit = set(tree.get_children()); selit = set(tree.selection())
        tree.selection_set(list(allit - selit))

    menu = tk.Menu(root, tearoff=0)
    menu.add_command(label="🔍 查看完整详情", command=show_details)
    menu.add_separator()
    menu.add_command(label="▶ 运行程序", command=run_file)
    menu.add_command(label="📂 打开所在文件夹", command=open_folder)
    menu.add_command(label="📋 复制完整路径", command=copy_path)
    menu.add_command(label="🔐 校验哈希 (SHA1)", command=check_hash)
    menu.add_separator()
    menu.add_command(label="🏷 批量编辑标签", command=batch_edit_tags)
    menu.add_command(label="📝 批量编辑备注", command=batch_edit_notes)
    menu.add_command(label="💾 导出选中详情", command=export_selected)
    menu.add_separator()
    menu.add_command(label="☑ 全选", command=select_all)
    menu.add_command(label="↔ 反选", command=invert_selection)
    menu.add_separator()
    menu.add_command(label="❌ 从索引移除", command=forget)

    def on_right_click(e):
        iid = tree.identify_row(e.y)
        if iid and iid not in tree.selection():
            tree.selection_set(iid)
        menu.tk_popup(e.x_root, e.y_root)

    tree.bind("<Double-1>", lambda e: show_details())
    tree.bind("<Return>", lambda e: show_details())
    tree.bind("<Delete>", lambda e: forget())
    tree.bind("<Button-3>", on_right_click)

    def _typing_focus():
        """焦点是否在输入框里：此时 Ctrl+A/C/E 应该留给输入框自己。"""
        try:
            w = root.focus_get()
        except Exception:
            return False
        if w is None:
            return False
        try:
            return w.winfo_class() in ("Entry", "TEntry", "Text", "TCombobox",
                                       "Spinbox", "TSpinbox")
        except Exception:
            return False

    def _hotkey(fn):
        def handler(event=None):
            if _typing_focus():
                return None          # 放行给 Entry/Text 的默认绑定
            fn()
            return "break"
        return handler

    root.bind("<Control-c>", _hotkey(copy_path))
    root.bind("<Control-f>", _hotkey(lambda: (search_var.set(""), search_entry.focus_set())))
    root.bind("<Control-a>", _hotkey(select_all))
    root.bind("<Control-e>", _hotkey(export_selected))

    # ===== 后台检测已安装 =====
    def bg_check_installed():
        nonlocal installed_raw, installed_norm
        installed_raw = get_installed_software()
        # 同时存 _norm_sw 与 _stem 两种形态：前者精确，后者能吃掉 winrar-x64-624scp 这类尾部噪音
        norms = set()
        for s in installed_raw:
            if not s:
                continue
            norms.add(_norm_sw(s))
            stem = _stem(s)
            if len(stem) >= 4:
                norms.add(stem)
        installed_norm = {n for n in norms if n}
        root.after(0, lambda: status_var.set(
            f"就绪。已自动检测本机安装的 {len(installed_raw)} 个软件。双击查看详情 | 拖拽文件快速打标签"))
        root.after(0, lambda: (_sw_cache_key.__setitem__("k", None), refresh()))
    threading.Thread(target=bg_check_installed, daemon=True).start()

    # ===== 扫描 =====
    def do_scan():
        roots = [d.strip() for d in re.split(r"[;\n]", dir_var.get())
                 if d.strip() and os.path.isdir(d.strip())]
        if not roots:
            messagebox.showwarning("提示", "请先选择要扫描的文件夹！"); return
        btn_scan.config(state="disabled")

        def prog(i, total, p):
            root.after(0, lambda: status_var.set(f"正在扫描 ({i}/{total}): {p[:100]}"))

        def log(m):
            root.after(0, lambda: status_var.set(m))

        def done():
            btn_scan.config(state="normal")
            status_var.set(f"扫描完成！已自动检测本机安装的 {len(installed_raw)} 个软件。")
            _sw_cache_key["k"] = None
            refresh()

        def worker():
            try:
                scan(roots, progress=prog, log=log)
            except Exception as e:
                LOG.exception("scan error")
                # Python 在 except 块结束时会删掉 e，lambda 里再引用 str(e) 会 NameError，
                # 报错对话框根本弹不出来 —— 先把消息取出来再丢给 after。
                msg = str(e)
                root.after(0, lambda m=msg: messagebox.showerror("扫描出错", m))
            finally:
                root.after(0, done)
        threading.Thread(target=worker, daemon=True).start()
    btn_scan.config(command=do_scan)

    # ===== 初始化 =====
    setup_logging()
    theme_colors["current"] = apply_theme(root, dark_var.get())
    apply_side_colors()
    refresh()
    root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--build":
        print(f"正在打包 {APP_NAME}{APP_VERSION} ...")
        install_and_import("pyinstaller", "PyInstaller")
        script = os.path.abspath(__file__)
        cmd = [
            "pyinstaller", "--noconsole", "--onefile", "--clean",
            "--name", f"PackManager{APP_VERSION}",
            "--collect-all", "tkinterdnd2",
            "--collect-all", "py7zr",
            "--hidden-import", "rarfile",
        ]
        icon = os.path.join(APP_DIR, "app.ico")
        if os.path.exists(icon):
            cmd += ["--icon", icon,
                    "--add-data", f"{icon}{os.pathsep}."]
        verfile = os.path.join(APP_DIR, "version_info.txt")
        if os.path.exists(verfile):
            cmd += ["--version-file", verfile]
        cmd.append(script)
        rc = subprocess.run(cmd, check=False).returncode
        exe = os.path.join(APP_DIR, "dist", f"PackManager{APP_VERSION}.exe")
        if rc != 0:
            print(f"打包失败（PyInstaller 退出码 {rc}），请检查上面的日志。")
            sys.exit(rc)
        if not os.path.exists(exe):
            print(f"PyInstaller 返回成功，但没找到产物：{exe}")
            sys.exit(1)
        print(f"打包完成：{exe}")
        sys.exit(0)
    run_gui()