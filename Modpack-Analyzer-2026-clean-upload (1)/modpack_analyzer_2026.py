#!/usr/bin/env python3
"""Modpack Analyzer 2026: анализатор сборок модов Minecraft.
Запуск: python modpack_analyzer_2026.py [папка_с_модами_или_jar-файлы ...]
"""
import io, json, os, queue, re, subprocess, sys, threading, zipfile
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

def _load_dnd():
    try:
        from tkinterdnd2 import TkinterDnD, DND_FILES
        return TkinterDnD, DND_FILES
    except Exception:
        return None, None


TkinterDnD, DND_FILES = _load_dnd()
if not DND_FILES and not getattr(sys, "frozen", False) and not os.environ.get("MODPACK_NO_AUTOINSTALL"):
    # Один раз ставим маленький пакет tkinterdnd2, чтобы папку можно было просто перетащить в окно.
    # Отключить: переменная окружения MODPACK_NO_AUTOINSTALL=1
    try:
        print("Устанавливаю tkinterdnd2 для перетаскивания папок (один раз)…")
        subprocess.run([sys.executable, "-m", "pip", "install", "--user", "-q", "tkinterdnd2"],
                       check=True, timeout=180)
        import importlib, site
        importlib.invalidate_caches()
        if site.getusersitepackages() not in sys.path:
            sys.path.append(site.getusersitepackages())
        TkinterDnD, DND_FILES = _load_dnd()
    except Exception:
        pass
Base = TkinterDnD.Tk if DND_FILES else tk.Tk

SKIP_DIRS = {"libraries", "versions", "runtime", "assets", "logs", "crash-reports", "saves"}

BUILTIN = {"minecraft", "java", "forge", "neoforge", "fabricloader", "fabric_loader",
           "fabric-loader", "quilt_loader", "quilt-loader"}
PAIRS = [("optifine", "sodium", "OptiFine несовместим с Sodium"),
         ("optifine", "embeddium", "OptiFine несовместим с Embeddium"),
         ("optifine", "iris", "OptiFine несовместим с Iris"),
         ("rubidium", "embeddium", "Rubidium и Embeddium выполняют одну задачу, нужен только один")]


# ---------- версии ----------
def vn(s):
    return [int(x) for x in re.findall(r"\d+", str(s or ""))]


def cmp(a, b):
    for i in range(max(len(a), len(b))):
        x = a[i] if i < len(a) else 0
        y = b[i] if i < len(b) else 0
        if x != y:
            return -1 if x < y else 1
    return 0


def fabric_sat(r, ver):
    if isinstance(r, list):
        return any(fabric_sat(x, ver) for x in r)
    r = str(r or "").strip()
    v = vn(ver)
    if not r or r == "*" or not v:
        return True

    def pred(p):
        m = re.match(r"^(>=|<=|>|<|=|~|\^)?(.*)$", p)
        op, s = m.group(1) or "=", m.group(2)
        if s in ("", "*"):
            return True
        if re.search(r"[xX*]", s):
            pre = [int(x) for x in s.split(".") if not re.search(r"[xX*]", x) and x.isdigit()]
            return all((v[i] if i < len(v) else 0) == n for i, n in enumerate(pre))
        a = vn(s)
        if not a:
            return True
        if op == ">=": return cmp(v, a) >= 0
        if op == ">": return cmp(v, a) > 0
        if op == "<=": return cmp(v, a) <= 0
        if op == "<": return cmp(v, a) < 0
        if op == "~":
            hi = (a + [0, 0])[:2]
            hi[1] += 1
            return cmp(v, a) >= 0 and cmp(v, hi) < 0
        if op == "^": return cmp(v, a) >= 0 and cmp(v, [a[0] + 1]) < 0
        return cmp(v, a) == 0

    return any(all(pred(p) for p in alt.split()) for alt in re.split(r"\s*\|\|\s*", r))


def forge_sat(r, ver):
    r = str(r or "").strip()
    v = vn(ver)
    if not r or not v:
        return True
    m = re.match(r"^([\[(])\s*([^,\])]*?)\s*(?:,\s*([^\])]*?))?\s*([\])])$", r)
    if not m:
        return True if re.search(r"[\[(]", r) else cmp(v, vn(r)) >= 0
    lb, lo, hi, rb = m.groups()
    if hi is None:
        return cmp(v, vn(lo)) == 0
    if lo:
        c = cmp(v, vn(lo))
        if c < 0 or (c == 0 and lb == "("): return False
    if hi:
        c = cmp(v, vn(hi))
        if c > 0 or (c == 0 and rb == ")"): return False
    return True


def sat(rec, rng, ver):
    return (fabric_sat if re.search("Fabric|Quilt", rec["loader"]) else forge_sat)(rng, ver)


def show_range(r):
    return " или ".join(r) if isinstance(r, list) else str(r)


# ---------- чтение JAR ----------
def parse_toml(t):
    mods, deps, cur = [], {}, None
    for line in t.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        h = re.match(r"^\[\[(.+?)\]\]", line)
        if h:
            n = h.group(1).strip()
            if n == "mods":
                cur = {}
                mods.append(cur)
            elif n.startswith("dependencies."):
                cur = {}
                deps.setdefault(n[13:].strip('"'), []).append(cur)
            else:
                cur = None
            continue
        if line.startswith("["):
            continue
        kv = re.match(r"^([A-Za-z_]\w*)\s*=\s*(.+)$", line)
        if kv and cur is not None:
            cur[kv.group(1)] = re.sub(r"\s+#.*$", "", kv.group(2)).strip().strip("\"'")
    return mods, deps


def pid(p):
    return p if isinstance(p, str) else p.get("id")


def read_jar(path):
    name = os.path.basename(path)
    base = dict(file=name, size=os.path.getsize(path), classes=0, mixins=0,
                deps=[], breaks=[], provides=[], env="", bad=False, nometa=False)
    try:
        zf = zipfile.ZipFile(path)
    except Exception:
        return [dict(base, loader="Ошибка", id=name, name=name, ver="?", bad=True)]
    names = zf.namelist()
    base["classes"] = sum(n.endswith(".class") for n in names)
    base["mixins"] = sum(bool(re.search(r"mixins?[^/]*\.json$", n, re.I)) for n in names)

    def txt(n):
        return zf.read(n).decode("utf-8-sig", "replace") if n in names else None

    def nested(prefix):
        for n in [n for n in names if n.startswith(prefix) and n.endswith(".jar")][:150]:
            try:
                yield zipfile.ZipFile(io.BytesIO(zf.read(n)))
            except Exception:
                pass

    out = []
    try:
        t = txt("fabric.mod.json")
        if t:
            m = json.loads(t, strict=False)
            rec = dict(base, loader="Fabric", id=m["id"], name=m.get("name") or m["id"], ver=m.get("version"),
                       deps=[dict(id=i, range=r, required=True) for i, r in (m.get("depends") or {}).items()],
                       breaks=[dict(id=i, range=r) for i, r in (m.get("breaks") or {}).items()],
                       provides=[pid(p) for p in m.get("provides") or []],
                       env=m.get("environment") if m.get("environment") not in (None, "*") else "")
            for z in nested("META-INF/jars/"):
                if "fabric.mod.json" in z.namelist():
                    try:
                        nm = json.loads(z.read("fabric.mod.json").decode("utf-8-sig"), strict=False)
                        rec["provides"] += [nm["id"]] + [pid(p) for p in nm.get("provides") or []]
                    except Exception:
                        pass
            out.append(rec)
        elif txt("quilt.mod.json"):
            q = json.loads(txt("quilt.mod.json"), strict=False).get("quilt_loader", {})
            deps = [dict(id=d, range="*", required=True) if isinstance(d, str)
                    else dict(id=d.get("id"), range=d.get("versions", "*"), required=not d.get("optional"))
                    for d in q.get("depends", [])]
            out.append(dict(base, loader="Quilt", id=q["id"], name=q.get("metadata", {}).get("name") or q["id"],
                            ver=q.get("version"), deps=deps, provides=[pid(p) for p in q.get("provides", [])]))
        else:
            tn = next((n for n in ("META-INF/neoforge.mods.toml", "META-INF/mods.toml") if n in names), None)
            if tn:
                loader = "NeoForge" if "neoforge" in tn else "Forge"
                mods, deps = parse_toml(txt(tn))
                prov = []
                for z in nested("META-INF/jarjar/"):
                    for n in ("META-INF/mods.toml", "META-INF/neoforge.mods.toml"):
                        if n in z.namelist():
                            prov += re.findall(r'modId\s*=\s*"([^"]+)"', z.read(n).decode("utf-8", "replace"))
                for i, m in enumerate(mods):
                    if "modId" not in m:
                        continue
                    ds = []
                    for d in deps.get(m["modId"], []):
                        req = d["mandatory"] == "true" if "mandatory" in d else d.get("type", "required") == "required"
                        ds.append(dict(id=d.get("modId"), range=d.get("versionRange", ""), required=req))
                    out.append(dict(base, size=0 if i else base["size"], loader=loader, id=m["modId"],
                                    name=m.get("displayName") or m["modId"], ver=m.get("version"), deps=ds, provides=prov))
            elif txt("mcmod.info"):
                j = json.loads(txt("mcmod.info"), strict=False)
                j = j.get("modList", []) if isinstance(j, dict) else j
                for m in j:
                    if m.get("modid"):
                        ds = [dict(id=str(d).split("@")[0], range="", required=True)
                              for d in m.get("requiredMods") or m.get("dependencies") or []]
                        out.append(dict(base, loader="Forge (старый)", id=m["modid"], name=m.get("name") or m["modid"],
                                        ver=m.get("version"), deps=ds))
    except Exception:
        return [dict(base, loader="Ошибка", id=name, name=name, ver="?", bad=True)]
    return out or [dict(base, loader="—", id=name, name=name, ver="?", nometa=True)]


# ---------- анализ ----------
def analyze(recs, target):
    iss = []
    add = lambda s, t, d: iss.append((s, t, d))
    good = [r for r in recs if not r["bad"] and not r["nometa"]]
    by_id, provided = {}, set(BUILTIN)
    for r in good:
        by_id.setdefault(r["id"], []).append(r)
        provided.add(r["id"])
        provided.update(p for p in r["provides"] if p)
    if "fabric-api" in provided:
        provided.add("fabric")
    for r in recs:
        if r["bad"]:
            add("err", "Файл не удалось прочитать", f"{r['file']}: повреждён или не является JAR-архивом.")
    for i, l in by_id.items():
        files = sorted({r["file"] for r in l})
        if len(files) > 1:
            add("err", f"Дубликат мода «{i}»", "Лежит в нескольких файлах: " + ", ".join(files) + ". Оставьте один.")
    grp = lambda r: "Fabric" if re.search("Fabric|Quilt", r["loader"]) else ("Forge" if r["loader"].startswith("Forge") else r["loader"])
    cnt = {}
    for r in good:
        cnt[grp(r)] = cnt.get(grp(r), 0) + 1
    if len(cnt) > 1:
        dom = max(cnt, key=cnt.get)
        add("err", "В сборке смешаны загрузчики", f"Основной загрузчик: {dom}. Не подходят: " +
            ", ".join(f"{r['name']} ({r['loader']})" for r in good if grp(r) != dom) + ".")
    for r in good:
        for d in r["deps"]:
            if d["id"] in BUILTIN or not d["required"]:
                continue
            if d["id"] not in provided and not (d["id"].startswith("fabric-") and "fabric-api" in provided):
                add("err", f"Не хватает зависимости «{d['id']}»",
                    f"Для мода «{r['name']}»" + (f" нужна версия {show_range(d['range'])}" if d["range"] else "") +
                    ". Возможно, она вложена в другой JAR, но среди найденных её нет.")
                continue
            o = (by_id.get(d["id"]) or [None])[0]
            if o and d["range"] and not sat(r, d["range"], o["ver"]):
                add("warn", f"Версия «{d['id']}» не подходит",
                    f"«{r['name']}» требует {show_range(d['range'])}, установлена {o['ver']}.")
        for b in r["breaks"]:
            o = (by_id.get(b["id"]) or [None])[0]
            if o and sat(r, b["range"], o["ver"]):
                add("err", f"Конфликт: «{r['name']}» и «{o['name']}»",
                    f"Мод «{r['name']}» объявляет, что ломается вместе с «{b['id']}» версии {o['ver']}.")
        if target:
            d = next((d for d in r["deps"] if d["id"] == "minecraft"), None)
            if d and d["range"] and not sat(r, d["range"], target):
                add("err", f"«{r['name']}» не для Minecraft {target}", f"Мод поддерживает: {show_range(d['range'])}.")
    has = lambda x: any(r["id"] == x or x in r["file"].lower() for r in recs)
    for a, b, msg in PAIRS:
        if has(a) and has(b):
            add("warn", "Известный конфликт", msg + ".")
    cl = [r for r in good if r["env"] == "client"]
    if cl:
        add("info", f"Моды только для клиента: {len(cl)}",
            "Их нужно убрать при запуске сервера: " + ", ".join(r["name"] for r in cl) + ".")
    nm = [r for r in recs if r["nometa"]]
    if nm:
        add("info", f"Нет метаданных мода: {len(nm)}", ", ".join(r["file"] for r in nm) +
            " не содержат fabric.mod.json или mods.toml. Это может быть OptiFine, библиотека или старый мод.")
    return sorted(iss, key=lambda i: {"err": 0, "warn": 1, "info": 2}[i[0]])


def fmt(b):
    return f"{b / 1048576:.1f} МБ" if b >= 1048576 else f"{max(1, round(b / 1024))} КБ"


# ---------- поиск модов ----------
def find_jars(paths):
    """Принимает файлы и папки (в том числе сборку целиком), сам находит моды."""
    out = []
    for p in paths:
        if os.path.isfile(p):
            if p.lower().endswith(".jar"):
                out.append(p)
            continue
        in_mods, every = [], []
        for d, dirs, files in os.walk(p):
            dirs[:] = [x for x in dirs if x.lower() not in SKIP_DIRS]
            js = [os.path.join(d, f) for f in sorted(files) if f.lower().endswith(".jar")]
            every += js
            if "mods" in os.path.relpath(d, p).lower().split(os.sep):
                in_mods += js
        out += in_mods or every
    return list(dict.fromkeys(out))


def guess_mc(recs):
    """Версия Minecraft, под которую подходит больше всего модов (кандидаты берём из их требований)."""
    deps = [(r, d) for r in recs for d in r["deps"] if d["id"] == "minecraft" and d["range"]]
    cands = {m.group(0) for _, d in deps for m in re.finditer(r"1\.\d{1,2}(?:\.\d+)?", show_range(d["range"]))}
    if not cands:
        return None
    score = lambda v: (sum(sat(r, d["range"], v) for r, d in deps), v.count("."), vn(v))
    return max(cands, key=score)


def default_mods_dir():
    home = os.path.expanduser("~")
    for c in (os.path.join(os.environ.get("APPDATA", ""), ".minecraft", "mods"),
              os.path.join(home, "Library", "Application Support", "minecraft", "mods"),
              os.path.join(home, ".minecraft", "mods")):
        if os.path.isdir(c):
            return c
    return None


# ---------- интерфейс ----------
APP_NAME, APP_VERSION = "Modpack Analyzer 2026", "1.0"
ICON_B64 = "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"
C = dict(bg="#14181a", panel="#1d2326", panel2="#2a3337", fg="#e8efe9", mute="#8a978f",
         ok="#5fbf5a", err="#ff6b5e", warn="#f2b84b", info="#6fb3e0", line="#34413f")
SEV = {"err": ("ОШИБКА", C["err"]), "warn": ("ВНИМАНИЕ", C["warn"]), "info": ("ИНФО", C["info"])}
LOADER_COL = {"Fabric": "#dbb98a", "Quilt": "#b58cf0", "Forge": "#7ea0d6", "NeoForge": "#f29b5c"}
COLS = [("name", "Название", 230), ("id", "ID", 170), ("ver", "Версия", 110), ("loader", "Загрузчик", 100),
        ("env", "Среда", 70), ("size", "Размер", 80), ("classes", "Классы", 70), ("mixins", "Mixin", 60)]


class App(Base):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.geometry("1100x780")
        self.minsize(860, 600)
        self.configure(bg=C["bg"])
        try:
            self.iconphoto(True, tk.PhotoImage(data=ICON_B64))
        except Exception:
            pass
        fam = set(tkfont.families())
        self.F = next((f for f in ("Segoe UI", "SF Pro Text", "Helvetica Neue", "Ubuntu", "DejaVu Sans") if f in fam), "TkDefaultFont")
        self.recs, self.issues, self.q, self.auto, self.tab = [], [], queue.Queue(), False, "issues"
        self.rev, self.wrap, self.sdata = {}, [], {}
        self.style()
        self.build()
        if DND_FILES:
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self.on_drop)
            self.dnd_bind("<<DropEnter>>", lambda e: (self.draw_zone(True), e.action)[1])
            self.dnd_bind("<<DropLeave>>", lambda e: (self.draw_zone(False), e.action)[1])
        self.bind_all("<MouseWheel>", self.wheel)
        self.bind_all("<Button-4>", lambda e: self.wheel(e, -1))
        self.bind_all("<Button-5>", lambda e: self.wheel(e, 1))
        self.after(100, self.poll)
        if len(sys.argv) > 1:
            self.load(sys.argv[1:])

    # --- оформление ---
    def style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("Treeview", background=C["panel"], fieldbackground=C["panel"], foreground=C["fg"],
                    rowheight=30, borderwidth=0, font=(self.F, 10),
                    bordercolor=C["bg"], lightcolor=C["bg"], darkcolor=C["bg"])
        s.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        s.configure("Treeview.Heading", background=C["panel2"], foreground=C["mute"], relief="flat",
                    font=(self.F, 10, "bold"), padding=7)
        s.map("Treeview", background=[("selected", "#2e4a33")], foreground=[("selected", C["fg"])])
        s.map("Treeview.Heading", background=[("active", C["line"])])
        s.configure("Vertical.TScrollbar", background=C["panel2"], troughcolor=C["bg"], bordercolor=C["bg"],
                    arrowcolor=C["mute"], relief="flat")
        s.map("Vertical.TScrollbar", background=[("active", C["line"]), ("!active", C["panel2"])])
        s.configure("g.Horizontal.TProgressbar", troughcolor=C["panel2"], background=C["ok"], bordercolor=C["bg"],
                    lightcolor=C["ok"], darkcolor=C["ok"], thickness=4)

    def lbl(self, parent, text, size=10, fg=None, bold=False, bg=None, **kw):
        return tk.Label(parent, text=text, bg=bg or C["bg"], fg=fg or C["fg"],
                        font=(self.F, size, "bold" if bold else "normal"), **kw)

    def btn(self, parent, text, cmd, primary=False):
        bg, fg = (C["ok"], "#0d1a0e") if primary else (C["panel2"], C["fg"])
        hov = "#7bd676" if primary else C["line"]
        b = tk.Label(parent, text=text, bg=bg, fg=fg, font=(self.F, 10, "bold"), padx=14, pady=8, cursor="hand2")
        b.bind("<Enter>", lambda e: b.configure(bg=hov))
        b.bind("<Leave>", lambda e: b.configure(bg=bg))
        b.bind("<Button-1>", lambda e: cmd())
        return b

    def build(self):
        hdr = tk.Frame(self, bg=C["bg"])
        hdr.pack(fill="x", padx=26, pady=(22, 6))
        left = tk.Frame(hdr, bg=C["bg"])
        left.pack(side="left")
        self.lbl(left, APP_NAME, 22, bold=True).pack(anchor="w")
        self.lbl(left, "Бросьте папку с модами. Зависимости, конфликты и версии проверятся сами.", 10, C["mute"]).pack(anchor="w")
        right = tk.Frame(hdr, bg=C["bg"])
        right.pack(side="right")
        self.btn(right, "Выбрать папку", self.pick_dir, True).pack(side="left", padx=4)
        self.btn(right, "Моя папка mods", self.pick_default).pack(side="left", padx=4)
        self.btn(right, "Файлы…", self.pick_files).pack(side="left", padx=4)

        row = tk.Frame(self, bg=C["bg"])
        row.pack(fill="x", padx=26, pady=(6, 0))
        self.lbl(row, "Версия Minecraft", 10, C["mute"]).pack(side="left")
        self.mc = tk.Entry(row, width=9, bg=C["panel2"], fg=C["fg"], insertbackground=C["fg"], relief="flat",
                           font=(self.F, 11), highlightthickness=1, highlightbackground=C["line"], highlightcolor=C["ok"])
        self.mc.pack(side="left", padx=8, ipady=4)
        self.mc.bind("<Key>", lambda e: setattr(self, "auto", False), add="+")
        self.mc.bind("<Return>", lambda e: self.refresh())
        self.auto_lbl = self.lbl(row, "", 9, C["ok"])
        self.auto_lbl.pack(side="left")
        self.btn(row, "Проверить заново", self.refresh).pack(side="left", padx=10)

        self.zone = tk.Canvas(self, bg=C["bg"], height=190, highlightthickness=0, cursor="hand2")
        self.zone.pack(fill="x", padx=26, pady=12)
        self.zone.bind("<Configure>", lambda e: self.draw_zone())
        self.zone.bind("<Button-1>", lambda e: self.pick_dir())

        self.prog = ttk.Progressbar(self, style="g.Horizontal.TProgressbar", mode="determinate")
        self.res = tk.Frame(self, bg=C["bg"])  # блок результатов, появляется после анализа
        self.verdict = self.lbl(self.res, "", 14, bold=True)
        self.verdict.pack(anchor="w", padx=26)
        self.cards = tk.Frame(self.res, bg=C["bg"])
        self.cards.pack(fill="x", padx=26, pady=10)

        bar = tk.Frame(self.res, bg=C["bg"])
        bar.pack(fill="x", padx=26)
        self.tabs = {}
        for key, title in (("issues", "Проблемы"), ("mods", "Моды"), ("stats", "Статистика")):
            t = self.lbl(bar, title, 11, C["mute"], True, padx=4, pady=6, cursor="hand2")
            t.pack(side="left", padx=(0, 18))
            t.bind("<Button-1>", lambda e, k=key: self.show(k))
            self.tabs[key] = t
        self.btn(bar, "Сохранить отчёт", self.save).pack(side="right")
        self.btn(bar, "Копировать отчёт", self.copy).pack(side="right", padx=8)
        tk.Frame(self.res, bg=C["line"], height=1).pack(fill="x", padx=26)

        self.body = tk.Frame(self.res, bg=C["bg"])
        self.body.pack(fill="both", expand=True, padx=26, pady=(10, 4))
        self.views = {}
        # проблемы
        v = tk.Frame(self.body, bg=C["bg"])
        self.ic = tk.Canvas(v, bg=C["bg"], highlightthickness=0)
        sb = ttk.Scrollbar(v, command=self.ic.yview)
        self.ic.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.ic.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.ic, bg=C["bg"])
        self.iw = self.ic.create_window(0, 0, window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.ic.configure(scrollregion=self.ic.bbox("all")))
        self.ic.bind("<Configure>", self.on_ic)
        self.views["issues"] = v
        # моды
        v = tk.Frame(self.body, bg=C["bg"])
        self.fvar = tk.StringVar()
        self.fvar.trace_add("write", lambda *a: self.fill_tree())
        tk.Entry(v, textvariable=self.fvar, bg=C["panel2"], fg=C["fg"], insertbackground=C["fg"], relief="flat",
                 font=(self.F, 11), highlightthickness=1, highlightbackground=C["line"],
                 highlightcolor=C["ok"]).pack(fill="x", pady=(0, 8), ipady=6)
        tf = tk.Frame(v, bg=C["bg"])
        tf.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tf, columns=[c[0] for c in COLS], show="headings")
        for k, t, w in COLS:
            self.tree.heading(k, text=t, anchor="e" if k in ("size", "classes", "mixins") else "w",
                              command=lambda k=k: self.sort(k))
            self.tree.column(k, width=w, anchor="e" if k in ("size", "classes", "mixins") else "w")
        self.tree.tag_configure("odd", background="#222a2d")
        sb = ttk.Scrollbar(tf, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.views["mods"] = v
        # статистика
        v = tk.Frame(self.body, bg=C["bg"])
        self.chips = tk.Frame(v, bg=C["bg"])
        self.chips.pack(fill="x", pady=(2, 10))
        g = tk.Frame(v, bg=C["bg"])
        g.pack(fill="both", expand=True)
        g.columnconfigure((0, 1), weight=1, uniform="a")
        g.rowconfigure(1, weight=1)
        self.lbl(g, "Самые большие по размеру", 10, C["mute"], True).grid(row=0, column=0, sticky="w")
        self.lbl(g, "Больше всего классов (грубая оценка нагрузки)", 10, C["mute"], True).grid(row=0, column=1, sticky="w", padx=(16, 0))
        self.c1 = tk.Canvas(g, bg=C["bg"], highlightthickness=0)
        self.c2 = tk.Canvas(g, bg=C["bg"], highlightthickness=0)
        self.c1.grid(row=1, column=0, sticky="nsew")
        self.c2.grid(row=1, column=1, sticky="nsew", padx=(16, 0))
        for c in (self.c1, self.c2):
            c.bind("<Configure>", lambda e: self.draw_stats())
        self.views["stats"] = v

        self.status = tk.StringVar(value="Папка не выбрана.")
        tk.Label(self, textvariable=self.status, bg=C["bg"], fg=C["mute"], font=(self.F, 9), anchor="w").pack(
            side="bottom", fill="x", padx=26, pady=(0, 10))

    # --- зона перетаскивания ---
    def draw_zone(self, hot=False):
        c = self.zone
        c.delete("all")
        w, h = c.winfo_width(), int(c["height"])
        c.create_rectangle(3, 3, w - 3, h - 3, outline=C["ok"] if hot else C["line"], dash=(8, 6), width=2,
                           fill="#1f2e22" if hot else C["panel"])
        if self.recs:
            c.create_text(w / 2, h / 2, text="Бросьте другую папку, чтобы заменить анализ", fill=C["mute"], font=(self.F, 11))
            return
        c.create_text(w / 2, h / 2 - 26, text="Бросьте папку с модами сюда", fill=C["fg"], font=(self.F, 20, "bold"))
        sub = "или нажмите, чтобы выбрать. Можно кинуть папку сборки целиком: mods найдётся сам."
        if not DND_FILES:
            sub = "Нажмите, чтобы выбрать папку. Для перетаскивания: pip install tkinterdnd2 (или киньте папку на файл скрипта)."
        c.create_text(w / 2, h / 2 + 14, text=sub, fill=C["mute"], font=(self.F, 11), width=max(200, w - 80))

    def on_drop(self, e):
        self.draw_zone(False)
        self.load(list(self.tk.splitlist(e.data)))

    # --- вкладки и прокрутка ---
    def show(self, key):
        self.tab = key
        for k, v in self.views.items():
            v.pack_forget()
            self.tabs[k].configure(fg=C["ok"] if k == key else C["mute"])
        self.views[key].pack(fill="both", expand=True)
        if key == "stats":
            self.after(30, self.draw_stats)

    def on_ic(self, e):
        self.ic.itemconfigure(self.iw, width=e.width - 4)
        for d in self.wrap:
            d.configure(wraplength=max(200, e.width - 120))

    def wheel(self, e, d=None):
        if self.tab == "issues" and self.res.winfo_ismapped():
            step = d if d else (-1 if e.delta > 0 else 1)
            self.ic.yview_scroll(step * 2, "units")

    # --- выбор источника ---
    def pick_files(self):
        p = filedialog.askopenfilenames(filetypes=[("Моды", "*.jar")])
        if p: self.load(list(p))

    def pick_dir(self):
        p = filedialog.askdirectory()
        if p: self.load([p])

    def pick_default(self):
        d = default_mods_dir()
        if d: self.load([d])
        else: messagebox.showinfo("Не найдено", "Стандартная папка mods не найдена. Выберите папку вручную.")

    def load(self, paths):
        jars = find_jars(paths)
        if not jars:
            messagebox.showinfo("Нет модов", "В выбранном месте нет JAR-файлов.")
            return
        self.prog.configure(maximum=len(jars), value=0)
        self.prog.pack(fill="x", padx=26, after=self.zone)

        def work():
            recs = []
            for i, p in enumerate(jars, 1):
                self.q.put(("p", i, len(jars), os.path.basename(p)))
                recs += read_jar(p)
            self.q.put(("done", recs))

        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        try:
            while True:
                m = self.q.get_nowait()
                if m[0] == "p":
                    self.prog.configure(value=m[1])
                    self.status.set(f"Читаю {m[1]} из {m[2]}: {m[3]}")
                else:
                    self.recs = m[1]
                    self.prog.pack_forget()
                    best = guess_mc(self.recs)
                    if best and (not self.mc.get() or self.auto):
                        self.mc.delete(0, "end")
                        self.mc.insert(0, best)
                        self.auto = True
                    self.refresh()
        except queue.Empty:
            pass
        self.after(100, self.poll)

    # --- показ результатов ---
    def card(self, big, small, col=None):
        f = tk.Frame(self.cards, bg=C["panel"], padx=18, pady=12)
        f.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.lbl(f, str(big), 24, col, True, bg=C["panel"]).pack(anchor="w")
        self.lbl(f, small, 10, C["mute"], bg=C["panel"]).pack(anchor="w")

    def refresh(self):
        if not self.recs:
            return
        self.issues = analyze(self.recs, self.mc.get().strip())
        e = sum(i[0] == "err" for i in self.issues)
        w = sum(i[0] == "warn" for i in self.issues)
        self.auto_lbl.configure(text="определена автоматически" if self.auto else "")
        self.zone.configure(height=64)
        self.draw_zone()
        if not self.res.winfo_manager():
            self.res.pack(fill="both", expand=True)
        if e: self.verdict.configure(text=f"Найдены ошибки: {e}. Сборка, скорее всего, не запустится.", fg=C["err"])
        elif w: self.verdict.configure(text=f"Критичного нет, предупреждений: {w}.", fg=C["warn"])
        else: self.verdict.configure(text="Сборка выглядит здоровой.", fg=C["ok"])
        for ch in self.cards.winfo_children():
            ch.destroy()
        self.card(len(self.recs), "модов")
        self.card(fmt(sum(r["size"] for r in self.recs)), "общий размер")
        self.card(e, "ошибок", C["err"] if e else C["ok"])
        self.card(w, "предупреждений", C["warn"] if w else C["ok"])
        self.status.set(f"Готово: {len(self.recs)} модов.")
        for ch in self.inner.winfo_children():
            ch.destroy()
        self.wrap = []
        if not self.issues:
            self.lbl(self.inner, "Проблем не найдено: зависимости на месте, дубликатов и конфликтов нет.", 12, C["ok"]).pack(anchor="w", pady=14)
        for sev, t, d in self.issues:
            lab, col = SEV[sev]
            card = tk.Frame(self.inner, bg=C["panel"])
            card.pack(fill="x", pady=5, padx=(0, 6))
            tk.Frame(card, bg=col, width=5).pack(side="left", fill="y")
            body = tk.Frame(card, bg=C["panel"])
            body.pack(side="left", fill="x", expand=True, padx=14, pady=10)
            top = tk.Frame(body, bg=C["panel"])
            top.pack(fill="x")
            tk.Label(top, text=lab, bg=col, fg="#111", font=(self.F, 8, "bold"), padx=7, pady=1).pack(side="left")
            self.lbl(top, t, 11, bold=True, bg=C["panel"]).pack(side="left", padx=10)
            dl = self.lbl(body, d, 10, C["mute"], bg=C["panel"], justify="left", anchor="w", wraplength=700)
            dl.pack(fill="x", pady=(6, 0))
            self.wrap.append(dl)
        self.fill_tree()
        self.draw_chips()
        self.show(self.tab if self.tab in self.views else "issues")

    def fill_tree(self, recs=None):
        q = self.fvar.get().lower().strip()
        recs = recs if recs is not None else sorted(self.recs, key=lambda r: -r["size"])
        self.tree.delete(*self.tree.get_children())
        i = 0
        for r in recs:
            if q and q not in (str(r["name"]) + " " + str(r["id"])).lower():
                continue
            self.tree.insert("", "end", tags=("odd",) if i % 2 else (), values=(
                r["name"], r["id"], r["ver"], r["loader"], r["env"] or "любая", fmt(r["size"]), r["classes"], r["mixins"]))
            i += 1

    def sort(self, k):
        self.rev[k] = not self.rev.get(k, True)
        key = (lambda r: r[k]) if k in ("size", "classes", "mixins") else (lambda r: str(r[k]).lower())
        self.fill_tree(sorted(self.recs, key=key, reverse=self.rev[k]))

    def draw_chips(self):
        for ch in self.chips.winfo_children():
            ch.destroy()
        lc = {}
        for r in self.recs:
            lc[r["loader"]] = lc.get(r["loader"], 0) + 1
        for k, n in sorted(lc.items(), key=lambda x: -x[1]):
            col = LOADER_COL.get(k.split(" ")[0], C["mute"])
            tk.Label(self.chips, text=f"{k}  {n}", bg=col, fg="#111", font=(self.F, 10, "bold"), padx=10, pady=3).pack(side="left", padx=(0, 8))

    def draw_stats(self):
        if not self.recs:
            return
        top = lambda k: [(r["name"], r[k]) for r in sorted(self.recs, key=lambda r: -r[k])[:10]]
        for c, items, f, col in ((self.c1, top("size"), fmt, C["ok"]), (self.c2, top("classes"), str, C["info"])):
            c.delete("all")
            w = c.winfo_width()
            mx = max([v for _, v in items] + [1])
            for i, (n, v) in enumerate(items):
                y = 8 + i * 40
                c.create_text(2, y, text=str(n)[:34], anchor="nw", fill=C["fg"], font=(self.F, 10))
                c.create_text(w - 2, y, text=f(v), anchor="ne", fill=C["mute"], font=(self.F, 10))
                c.create_rectangle(2, y + 20, w - 2, y + 26, fill=C["panel2"], width=0)
                c.create_rectangle(2, y + 20, 2 + max(3, (w - 4) * v / mx), y + 26, fill=col, width=0)

    # --- отчёт ---
    def report(self):
        lab = {"err": "ошибка", "warn": "внимание", "info": "инфо"}
        body = "\n".join(f"[{lab[s]}] {t}: {d}" for s, t, d in self.issues) or "Проблем не найдено."
        return f"Отчёт по сборке ({len(self.recs)} модов)\n\n{body}\n"

    def copy(self):
        self.clipboard_clear()
        self.clipboard_append(self.report())
        self.status.set("Отчёт скопирован в буфер обмена.")

    def save(self):
        p = filedialog.asksaveasfilename(defaultextension=".txt", filetypes=[("Текст", "*.txt")])
        if p:
            with open(p, "w", encoding="utf-8") as f:
                f.write(self.report())


if __name__ == "__main__":
    App().mainloop()
