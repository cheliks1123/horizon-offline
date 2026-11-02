"""Horizon Offline — launches Horizon without a Microsoft account.

Small Tkinter launcher around the official `horizon-ctl` command line. It finds
Horizon versions installed by the official launcher, downloads the Minecraft
files Horizon prepares its resources from (client, server, libraries, assets and
a Java runtime), runs `horizon-prepare`, and starts the game without signing in.

Run it with pythonw.exe (or `python horizon_offline.py --check` for diagnostics).
"""

from __future__ import annotations

import configparser
import concurrent.futures
import hashlib
import json
import lzma
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import urllib.request
from pathlib import Path
from tkinter import messagebox, ttk

APP_NAME = "Horizon Offline"
MENU_PLACEHOLDER = "— главное меню —"
CREATE_NO_WINDOW = 0x08000000
USER_AGENT = "HorizonOffline/1.1"
DOWNLOAD_WORKERS = 16

VERSION_MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
RESOURCES_URL = "https://resources.download.minecraft.net"
JAVA_RUNTIME_INDEX = (
    "https://launchermeta.mojang.com/v1/products/java-runtime/"
    "2ec0cc96c44e5a76b9c8b7c39df7210883d12871/all.json"
)


def app_dir() -> Path:
    """Folder of the script, or of the frozen executable."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = app_dir()
INI_PATH = APP_DIR / "launcher.ini"
APPDATA = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
VERSIONS_DIR = APPDATA / "horizon-launcher" / "versions"
SETTINGS_PATH = APPDATA / "horizon-launcher" / "settings.json"
DEFAULT_HOME = APPDATA / "horizon"
DEFAULT_MINECRAFT = APPDATA / ".minecraft"

DEFAULTS = {"nick": "", "version": "", "world": "", "home": "", "minecraft": ""}


# --- settings ---------------------------------------------------------------


def read_config() -> dict[str, str]:
    config = dict(DEFAULTS)
    parser = configparser.ConfigParser()
    try:
        parser.read(INI_PATH, encoding="utf-8")
    except (OSError, configparser.Error):
        return config
    if parser.has_section("launcher"):
        for key in config:
            value = parser.get("launcher", key, fallback="").strip()
            if value:
                config[key] = value
    return config


def write_config(config: dict[str, str]) -> None:
    parser = configparser.ConfigParser()
    parser["launcher"] = {key: config.get(key, "") for key in DEFAULTS}
    try:
        with INI_PATH.open("w", encoding="utf-8") as handle:
            handle.write("; Horizon Offline launcher settings\n")
            handle.write("; world: empty = main menu\n")
            parser.write(handle)
    except OSError:
        pass


# --- installed versions -----------------------------------------------------


def read_lock(version_dir: Path) -> dict | None:
    try:
        data = json.loads((version_dir / "research-lock.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def minecraft_version(version_dir: Path) -> str | None:
    lock = read_lock(version_dir)
    if not lock:
        return None
    try:
        return str(lock["minecraft"]["version"])
    except (KeyError, TypeError):
        return None


def is_prepared(version_dir: Path) -> bool:
    """A version the official launcher installed and horizon-prepare prepared."""
    if not (version_dir / "horizon-ctl.exe").is_file():
        return False
    version = minecraft_version(version_dir)
    if not version:
        return False
    resources = version_dir / ".local" / "horizon-client" / version / "horizon-resources.json"
    return resources.is_file()


def horizon_versions() -> list[Path]:
    """Every version the official launcher installed, prepared or not."""
    try:
        folders = [entry for entry in VERSIONS_DIR.iterdir() if entry.is_dir()]
    except OSError:
        return []
    usable = [
        entry
        for entry in folders
        if (entry / "horizon-ctl.exe").is_file() and minecraft_version(entry)
    ]
    stable = sorted((e for e in usable if e.name.startswith("v")), key=lambda e: e.name, reverse=True)
    nightly = sorted((e for e in usable if not e.name.startswith("v")), key=lambda e: e.name, reverse=True)
    return stable + nightly


def installed_versions() -> list[Path]:
    return [entry for entry in horizon_versions() if is_prepared(entry)]


def horizon_selected() -> str:
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    selected = data.get("selected") if isinstance(data, dict) else None
    return selected if isinstance(selected, str) else ""


def default_version(versions: list[Path], config: dict[str, str]) -> str:
    names = [entry.name for entry in versions]
    for candidate in (config.get("version", ""), horizon_selected()):
        if candidate in names:
            return candidate
    return names[0] if names else ""


# --- worlds -----------------------------------------------------------------


def game_home(config: dict[str, str]) -> Path:
    raw = config.get("home", "").strip()
    if raw:
        return Path(os.path.expandvars(os.path.expanduser(raw)))
    return DEFAULT_HOME


def minecraft_home(config: dict[str, str]) -> Path:
    raw = config.get("minecraft", "").strip()
    if raw:
        return Path(os.path.expandvars(os.path.expanduser(raw)))
    return DEFAULT_MINECRAFT


def list_worlds(home: Path) -> list[str]:
    try:
        folders = [entry for entry in (home / "saves").iterdir() if (entry / "level.dat").is_file()]
    except OSError:
        return []
    return sorted(entry.name for entry in folders)


def default_nick() -> str:
    for candidate in (DEFAULT_HOME / "horizon-player.txt", Path.home() / ".local/share/horizon/horizon-player.txt"):
        try:
            text = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            return text
    return "Player"


def launch_game(version_dir: Path, world: str, nick: str, home: Path) -> None:
    """Start horizon-ctl in its own window; it opens the game and waits for it."""
    args = [str(version_dir / "horizon-ctl.exe"), "launch"]
    if world:
        args += ["--world", world]
    if nick:
        args += ["--name", nick]
    env = os.environ.copy()
    env["HORIZON_HOME"] = str(home)
    subprocess.Popen(
        args,
        cwd=str(version_dir),
        env=env,
        creationflags=CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# --- downloading the Minecraft files ----------------------------------------


class DownloadError(Exception):
    """Something went wrong while downloading or preparing a version."""


def sha1_of(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def http_get(url: str, timeout: int = 60) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def fetch_json(url: str) -> dict:
    try:
        return json.loads(http_get(url).decode("utf-8"))
    except (OSError, ValueError) as error:
        raise DownloadError(f"не удалось прочитать {url}: {error}") from error


def http_download(url: str, dest: Path, expected_sha1: str | None, lzma_packed: bool = False) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha1()
    temp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(request, timeout=120) as response, temp.open("wb") as handle:
            while True:
                block = response.read(1 << 20)
                if not block:
                    break
                handle.write(block)
                digest.update(block)
        if lzma_packed:
            temp.write_bytes(lzma.decompress(temp.read_bytes(), format=lzma.FORMAT_AUTO))
            digest = hashlib.sha1(temp.read_bytes())
        if expected_sha1 and digest.hexdigest() != expected_sha1.lower():
            raise DownloadError(f"файл повреждён: {dest.name}")
        os.replace(temp, dest)
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


def rules_allow(entry: dict) -> bool:
    """Mojang's rule list: the last matching rule wins, no rules means allowed."""
    rules = entry.get("rules")
    if not rules:
        return True
    allowed = False
    for rule in rules:
        os_rule = rule.get("os") or {}
        name = os_rule.get("name")
        if name and name != "windows":
            continue
        arch = os_rule.get("arch")
        if arch and arch not in ("x86_64", "x64", "amd64"):
            continue
        if rule.get("features"):
            continue
        allowed = rule.get("action") == "allow"
    return allowed


class Job:
    """One file to download."""

    __slots__ = ("url", "path", "sha1", "size", "lzma")

    def __init__(self, url: str, path: Path, sha1: str | None, size: int, lzma_packed: bool = False) -> None:
        self.url = url
        self.path = path
        self.sha1 = sha1
        self.size = size
        self.lzma = lzma_packed

    def up_to_date(self) -> bool:
        try:
            if not self.path.is_file():
                return False
            if self.size > 0:
                return self.path.stat().st_size == self.size
            return bool(self.sha1) and sha1_of(self.path) == self.sha1.lower()
        except OSError:
            return False


def version_json(version: str, lock: dict, mc_dir: Path, log) -> dict:
    """The 26.3 (or whatever the lock pins) version JSON, downloaded if needed."""
    manifest = fetch_json(VERSION_MANIFEST_URL)
    entry = next((item for item in manifest.get("versions", []) if item.get("id") == version), None)
    if not entry:
        raise DownloadError(f"Minecraft {version} нет в манифесте Mojang")

    pinned = lock.get("minecraft") or {}
    accepted = {value.lower() for value in pinned.get("accepted_manifest_sha1") or []}
    if pinned.get("manifest_sha1"):
        accepted.add(str(pinned["manifest_sha1"]).lower())

    target = mc_dir / "versions" / version / f"{version}.json"
    if target.is_file():
        try:
            if not accepted or sha1_of(target) in accepted:
                return json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass

    log(f"Скачиваю описание версии {version}…")
    raw = http_get(entry["url"])
    if accepted and hashlib.sha1(raw).hexdigest() not in accepted:
        raise DownloadError(f"описание версии {version} не совпадает с research-lock.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    return json.loads(raw.decode("utf-8"))


def plan_minecraft(version_dir: Path, mc_dir: Path, log) -> tuple[list[Job], Path, Path]:
    """Work out every file Horizon needs; returns jobs, server jar and Java home."""
    lock = read_lock(version_dir)
    if not lock:
        raise DownloadError("в папке версии нет research-lock.json")
    pinned = lock.get("minecraft") or {}
    version = pinned.get("version")
    if not version:
        raise DownloadError("research-lock.json не указывает версию Minecraft")

    data = version_json(str(version), lock, mc_dir, log)
    downloads = data.get("downloads") or {}
    client = downloads.get("client") or {}
    server = downloads.get("server") or {}
    if not client.get("url") or not server.get("url"):
        raise DownloadError(f"у версии {version} нет ссылок на клиент или сервер")

    ver_dir = mc_dir / "versions" / version
    jobs: list[Job] = [
        Job(client["url"], ver_dir / f"{version}.jar", client.get("sha1"), int(client.get("size") or 0)),
        Job(server["url"], ver_dir / "server.jar", server.get("sha1"), int(server.get("size") or 0)),
    ]

    libraries = 0
    for library in data.get("libraries") or []:
        if not rules_allow(library):
            continue
        artifact = (library.get("downloads") or {}).get("artifact")
        if not artifact or not artifact.get("url"):
            continue
        relative = Path(*str(artifact.get("path", "")).split("/"))
        if not relative.name:
            continue
        jobs.append(
            Job(
                artifact["url"],
                mc_dir / "libraries" / relative,
                artifact.get("sha1"),
                int(artifact.get("size") or 0),
            )
        )
        libraries += 1

    asset_index = data.get("assetIndex") or {}
    index_id = str(asset_index.get("id") or "")
    index_path = mc_dir / "assets" / "indexes" / f"{index_id}.json"
    accepted_index = {value.lower() for value in pinned.get("accepted_asset_index_sha1") or []}
    if asset_index.get("sha1"):
        accepted_index.add(str(asset_index["sha1"]).lower())
    if not index_path.is_file() or (accepted_index and sha1_of(index_path) not in accepted_index):
        log("Скачиваю список ассетов…")
        raw = http_get(asset_index["url"])
        if accepted_index and hashlib.sha1(raw).hexdigest() not in accepted_index:
            raise DownloadError("список ассетов не совпадает с research-lock.json")
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_bytes(raw)
    index = json.loads(index_path.read_text(encoding="utf-8"))

    assets = 0
    for name, obj in (index.get("objects") or {}).items():
        if obj.get("virtual"):
            continue
        digest = str(obj.get("hash") or "")
        if len(digest) < 2:
            continue
        jobs.append(
            Job(
                f"{RESOURCES_URL}/{digest[:2]}/{digest}",
                mc_dir / "assets" / "objects" / digest[:2] / digest,
                digest,
                int(obj.get("size") or 0),
            )
        )
        assets += 1

    java_component = str((data.get("javaVersion") or {}).get("component") or "")
    java_home = mc_dir / "runtime" / java_component / "windows-x64" / java_component
    if java_component:
        java_count = plan_java(java_component, mc_dir, jobs, log)
    else:
        java_count = 0

    log(
        f"Версия {version}: библиотек {libraries}, ассетов {assets}, "
        f"файлов Java {java_count}, всего {len(jobs)} файлов"
    )
    return jobs, ver_dir / "server.jar", java_home


def plan_java(component: str, mc_dir: Path, jobs: list[Job], log) -> int:
    index = fetch_json(JAVA_RUNTIME_INDEX)
    for platform in ("windows-x64", "windows-x86"):
        entries = index.get(platform, {}).get(component)
        if entries:
            break
    else:
        log(f"Java {component} для Windows не найдена — оставляю системную")
        return 0

    manifest = fetch_json(entries[0]["manifest"]["url"])
    root = mc_dir / "runtime" / component / platform / component
    count = 0
    for name, entry in (manifest.get("files") or {}).items():
        if entry.get("type") == "directory":
            continue
        downloads = entry.get("downloads") or {}
        raw = downloads.get("raw")
        packed = downloads.get("lzma")
        chosen = raw or packed
        if not chosen or not chosen.get("url"):
            continue
        jobs.append(
            Job(
                chosen["url"],
                root / Path(*name.split("/")),
                chosen.get("sha1"),
                0 if raw is None else int(chosen.get("size") or 0),
                lzma_packed=raw is None,
            )
        )
        count += 1
    return count


def download_jobs(jobs: list[Job], progress, log) -> None:
    todo = [job for job in jobs if not job.up_to_date()]
    skipped = len(jobs) - len(todo)
    if skipped:
        log(f"Уже на месте: {skipped} файлов")
    if not todo:
        progress(len(jobs), len(jobs), "Все файлы на месте")
        return

    total = len(jobs)
    done = skipped
    failed: list[str] = []
    progress(done, total, f"Скачиваю {len(todo)} файлов…")

    with concurrent.futures.ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
        futures = {
            pool.submit(http_download, job.url, job.path, job.sha1, job.lzma): job for job in todo
        }
        for future in concurrent.futures.as_completed(futures):
            job = futures[future]
            try:
                future.result()
            except Exception as error:  # noqa: BLE001 - report every failure to the user
                failed.append(f"{job.path.name}: {error}")
            done += 1
            if done % 20 == 0 or done == total:
                progress(done, total, f"Скачано {done} из {total}")

    if failed:
        preview = "\n".join(failed[:5])
        raise DownloadError(f"не скачалось файлов: {len(failed)}\n{preview}")


def run_tool(program: Path, args: list[str], env: dict[str, str], log) -> None:
    log(f"$ {program.name} {' '.join(args)}")
    process = subprocess.Popen(
        [str(program)] + args,
        cwd=str(program.parent),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
    )
    assert process.stdout is not None
    for line in process.stdout:
        line = line.rstrip()
        if line:
            log(line)
    code = process.wait()
    if code != 0:
        raise DownloadError(f"{program.name} завершился с кодом {code}")


def prepare_version(version_dir: Path, mc_dir: Path, progress, log) -> None:
    """Download Minecraft's files and build Horizon's resources for this version."""
    if not (version_dir / "horizon-prepare.exe").is_file():
        raise DownloadError("в папке версии нет horizon-prepare.exe")
    log(f"Папка версии: {version_dir}")
    log(f"Файлы Minecraft: {mc_dir}")

    progress(0, 1, "Читаю манифест Mojang…")
    jobs, server_jar, java_home = plan_minecraft(version_dir, mc_dir, log)
    download_jobs(jobs, progress, log)

    env = os.environ.copy()
    env["HORIZON_MINECRAFT_DIR"] = str(mc_dir)
    env["HORIZON_SERVER_JAR"] = str(server_jar)
    if (java_home / "bin" / "java.exe").is_file():
        env["JAVA_HOME"] = str(java_home)

    progress(0, 1, "Готовлю ресурсы Horizon (может занять несколько минут)…")
    log("Собираю ресурсы Horizon…")
    run_tool(
        version_dir / "horizon-prepare.exe",
        [
            "--prepare",
            "--server-jar",
            str(server_jar),
            "--minecraft-dir",
            str(mc_dir),
            "--root",
            str(version_dir),
        ],
        env,
        log,
    )

    progress(0, 1, "Копирую звуки…")
    try:
        run_tool(
            version_dir / "horizon-prepare.exe",
            ["sounds", "--minecraft-dir", str(mc_dir), "--root", str(version_dir)],
            env,
            log,
        )
    except DownloadError as error:
        log(f"Звуки не скопировались (игра будет беззвучной): {error}")

    if not is_prepared(version_dir):
        raise DownloadError("ресурсы не появились — horizon-prepare не закончил работу")
    progress(1, 1, "Готово")


# --- UI ---------------------------------------------------------------------


class DownloadDialog(tk.Toplevel):
    """Shows the download and preparation of one Horizon version."""

    def __init__(self, master: tk.Misc, version_dir: Path, mc_dir: Path, on_finish) -> None:
        super().__init__(master)
        self.version_dir = version_dir
        self.mc_dir = mc_dir
        self.on_finish = on_finish
        self.events: queue.Queue = queue.Queue()
        self.running = True

        version = minecraft_version(version_dir) or "?"
        self.title(f"{APP_NAME} — скачивание {version}")
        self.resizable(False, False)
        self.configure(padx=16, pady=14)
        self.transient(master)

        ttk.Label(self, text=f"Minecraft: Java Edition {version}", font=("Segoe UI Semibold", 11)).pack(
            anchor="w"
        )
        ttk.Label(
            self,
            text="Файлы скачиваются с серверов Mojang, ресурсы собираются локально.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(2, 10))

        self.status_var = tk.StringVar(value="Подготовка…")
        ttk.Label(self, textvariable=self.status_var, style="Muted.TLabel").pack(anchor="w")
        self.bar = ttk.Progressbar(self, mode="determinate", length=560, maximum=100)
        self.bar.pack(fill="x", pady=(4, 10))

        self.text = tk.Text(self, height=11, width=78, state="disabled", wrap="word")
        self.text.pack(fill="both", expand=True)

        self.close_button = ttk.Button(self, text="Закрыть", command=self.close, state="disabled")
        self.close_button.pack(anchor="e", pady=(10, 0))

        self.protocol("WM_DELETE_WINDOW", self.close)
        threading.Thread(target=self.work, daemon=True).start()
        self.after(120, self.poll)
        self.grab_set()

    def work(self) -> None:
        def progress(done: int, total: int, text: str) -> None:
            self.events.put(("progress", done, total, text))

        def log(line: str) -> None:
            self.events.put(("log", line))

        try:
            prepare_version(self.version_dir, self.mc_dir, progress, log)
        except Exception as error:  # noqa: BLE001 - shown in the window
            self.events.put(("done", False, str(error)))
        else:
            self.events.put(("done", True, ""))

    def poll(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind = event[0]
            if kind == "progress":
                _, done, total, text = event
                self.bar["value"] = 100.0 * done / total if total else 0
                self.status_var.set(text)
            elif kind == "log":
                self.append(event[1])
            elif kind == "done":
                _, ok, error = event
                self.running = False
                self.close_button.configure(state="normal")
                if ok:
                    self.bar["value"] = 100
                    self.status_var.set("Готово — версия готова к запуску")
                    self.append("Готово.")
                else:
                    self.status_var.set("Ошибка")
                    self.append(f"Ошибка: {error}")
                    messagebox.showerror(APP_NAME, error, parent=self)
                self.on_finish(ok)
        if self.running:
            self.after(120, self.poll)

    def append(self, line: str) -> None:
        self.text.configure(state="normal")
        self.text.insert("end", line + "\n")
        self.text.see("end")
        self.text.configure(state="disabled")

    def close(self) -> None:
        if self.running:
            messagebox.showinfo(APP_NAME, "Дождись окончания скачивания.", parent=self)
            return
        self.destroy()


class Launcher(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.settings = read_config()
        self.versions = horizon_versions()
        self.mc_dir = minecraft_home(self.settings)
        self.home = game_home(self.settings)
        worlds = list_worlds(self.home)

        self.title(APP_NAME)
        self.resizable(False, False)
        self.configure(padx=18, pady=16)
        try:
            self.iconbitmap(default=str(APP_DIR / "icon.ico"))
        except tk.TclError:
            pass

        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Play.TButton", font=("Segoe UI Semibold", 10), padding=(20, 6))
        style.configure("Muted.TLabel", foreground="#6b6b6b")

        header = ttk.Frame(self)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        ttk.Label(header, text="Horizon", font=("Segoe UI Semibold", 18)).pack(side="left")
        ttk.Label(header, text="офлайн", style="Muted.TLabel").pack(side="left", padx=(8, 0), pady=(6, 0))

        self.nick_var = tk.StringVar(value=self.settings.get("nick") or default_nick())
        self.version_var = tk.StringVar(value=default_version(self.versions, self.settings))
        self.mode_var = tk.StringVar(value="menu")
        initial_world = self.settings.get("world", "")
        self.world_var = tk.StringVar(
            value=initial_world if initial_world in worlds else MENU_PLACEHOLDER
        )

        ttk.Label(self, text="Версия:").grid(row=1, column=0, sticky="w", pady=3)
        self.version_box = ttk.Combobox(
            self,
            textvariable=self.version_var,
            state="readonly",
            width=36,
            values=[entry.name for entry in self.versions],
        )
        self.version_box.grid(row=1, column=1, sticky="ew", pady=3)
        self.version_box.bind("<<ComboboxSelected>>", lambda _event: self.sync_version())

        ttk.Label(self, text="Ник:").grid(row=2, column=0, sticky="w", pady=3)
        nick_entry = ttk.Entry(self, textvariable=self.nick_var, width=38)
        nick_entry.grid(row=2, column=1, sticky="ew", pady=3)

        ttk.Label(self, text="Запуск:").grid(row=3, column=0, sticky="nw", pady=(7, 3))
        modes = ttk.Frame(self)
        modes.grid(row=3, column=1, sticky="w", pady=(7, 3))
        ttk.Radiobutton(
            modes, text="Главное меню", value="menu", variable=self.mode_var, command=self.sync_mode
        ).pack(side="left")
        ttk.Radiobutton(
            modes, text="Загрузить мир", value="world", variable=self.mode_var, command=self.sync_mode
        ).pack(side="left", padx=(14, 0))

        ttk.Label(self, text="Мир:").grid(row=4, column=0, sticky="w", pady=3)
        self.world_box = ttk.Combobox(
            self,
            textvariable=self.world_var,
            state="disabled",
            width=36,
            values=[MENU_PLACEHOLDER] + worlds,
        )
        self.world_box.grid(row=4, column=1, sticky="ew", pady=3)

        buttons = ttk.Frame(self)
        buttons.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        self.play_button = ttk.Button(buttons, text="Играть", style="Play.TButton", command=self.play)
        self.play_button.pack(side="left")
        self.download_button = ttk.Button(
            buttons, text="Скачать", command=self.download
        )
        self.download_button.pack(side="left", padx=(10, 0))
        ttk.Button(buttons, text="Папка миров", command=self.open_worlds).pack(side="left", padx=(10, 0))
        ttk.Button(buttons, text="Корневая папка", command=self.open_root).pack(side="left", padx=(10, 0))
        ttk.Button(buttons, text="Обновить", command=self.refresh).pack(side="left", padx=(10, 0))

        self.status_var = tk.StringVar(
            value=f"Версий: {len(self.versions)} · миров: {len(worlds)}"
        )
        ttk.Label(self, textvariable=self.status_var, style="Muted.TLabel").grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(12, 0)
        )

        self.columnconfigure(1, weight=1)
        self.sync_mode()
        self.sync_version()
        self.center()

        if not self.versions:
            self.status_var.set("Horizon не найден: сначала запусти официальный лаунчер")
        nick_entry.focus_set()
        self.bind("<Return>", lambda _event: self.play())

    def center(self) -> None:
        self.update_idletasks()
        width = self.winfo_width()
        height = self.winfo_height()
        x = max(0, (self.winfo_screenwidth() - width) // 2)
        y = max(0, (self.winfo_screenheight() - height) // 3)
        self.geometry(f"+{x}+{y}")

    def selected_version_dir(self) -> Path | None:
        name = self.version_var.get()
        if not name:
            return None
        return VERSIONS_DIR / name

    def sync_version(self) -> None:
        """Keep the buttons and the hint in step with the selected version."""
        version_dir = self.selected_version_dir()
        if version_dir is None:
            self.play_button.configure(state="disabled")
            self.download_button.configure(state="disabled", text="Скачать")
            return
        version = minecraft_version(version_dir) or "?"
        if is_prepared(version_dir):
            self.play_button.configure(state="normal")
            self.download_button.configure(state="normal", text=f"Пересобрать {version}")
            self.status_var.set(f"Версия {version} готова")
        else:
            self.play_button.configure(state="disabled")
            self.download_button.configure(state="normal", text=f"Скачать {version}")
            self.status_var.set(f"Версия {version} не подготовлена — нажми «Скачать {version}»")

    def sync_mode(self) -> None:
        state = "readonly" if self.mode_var.get() == "world" else "disabled"
        self.world_box.configure(state=state)

    def refresh(self) -> None:
        self.settings = read_config()
        self.versions = horizon_versions()
        names = [entry.name for entry in self.versions]
        self.version_box.configure(values=names)
        if self.version_var.get() not in names:
            self.version_var.set(default_version(self.versions, self.settings))
        self.mc_dir = minecraft_home(self.settings)
        self.home = game_home(self.settings)
        worlds = list_worlds(self.home)
        self.world_box.configure(values=[MENU_PLACEHOLDER] + worlds)
        if self.world_var.get() not in [MENU_PLACEHOLDER] + worlds:
            self.world_var.set(MENU_PLACEHOLDER)
        self.sync_version()

    def open_worlds(self) -> None:
        saves = self.home / "saves"
        try:
            saves.mkdir(parents=True, exist_ok=True)
            os.startfile(str(saves))  # type: ignore[attr-defined]
        except OSError as error:
            messagebox.showerror(APP_NAME, f"Не удалось открыть папку миров:\n{error}")

    def open_root(self) -> None:
        """Open the game folder: worlds, options, resourcepacks and mods live here."""
        mods = self.home / "horizon-mods"
        try:
            self.home.mkdir(parents=True, exist_ok=True)
            mods.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            messagebox.showerror(APP_NAME, f"Не удалось создать корневую папку:\n{error}")
            return
        try:
            # Open the root with horizon-mods selected, so it is obvious where mods go.
            subprocess.Popen(["explorer", "/select,", str(mods)], creationflags=CREATE_NO_WINDOW)
        except OSError:
            try:
                os.startfile(str(self.home))  # type: ignore[attr-defined]
            except OSError as error:
                messagebox.showerror(APP_NAME, f"Не удалось открыть корневую папку:\n{error}")
                return
        self.status_var.set(f"Моды (.hmod) кладутся в {mods}")

    def download(self) -> None:
        version_dir = self.selected_version_dir()
        if version_dir is None:
            messagebox.showerror(
                APP_NAME,
                "Horizon не найден.\n\n"
                "Запусти официальный лаунчер Horizon, он установит версию игры; "
                "этот лаунчер потом скачает для неё файлы Minecraft.",
            )
            return
        self.download_button.configure(state="disabled")
        DownloadDialog(self, version_dir, self.mc_dir, self.after_download)

    def after_download(self, ok: bool) -> None:
        self.refresh()
        if ok:
            self.download_button.configure(state="normal")

    def play(self) -> None:
        version = self.version_var.get()
        version_dir = VERSIONS_DIR / version if version else None
        if not version or version_dir is None or not is_prepared(version_dir):
            messagebox.showerror(
                APP_NAME,
                "Версия Horizon не подготовлена.\n\n"
                "Нажми «Скачать» — лаунчер сам скачает файлы Minecraft "
                "и соберёт ресурсы Horizon.",
            )
            return

        world = ""
        if self.mode_var.get() == "world":
            world = self.world_var.get()
            if not world or world == MENU_PLACEHOLDER:
                messagebox.showerror(APP_NAME, "Выбери мир или переключись на «Главное меню».")
                return

        nick = self.nick_var.get().strip()
        try:
            launch_game(version_dir, world, nick, self.home)
        except OSError as error:
            messagebox.showerror(APP_NAME, f"Не удалось запустить игру:\n{error}")
            return

        write_config(
            {
                "nick": nick,
                "version": version,
                "world": world,
                "home": self.settings.get("home", ""),
                "minecraft": self.settings.get("minecraft", ""),
            }
        )
        self.status_var.set("Запускаю…")
        self.play_button.configure(state="disabled")
        self.after(1500, self.destroy)


def enable_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def check() -> int:
    settings = read_config()
    versions = horizon_versions()
    print(
        json.dumps(
            {
                "versions": [entry.name for entry in versions],
                "default_version": default_version(versions, settings),
                "prepared": {entry.name: is_prepared(entry) for entry in versions},
                "minecraft": {
                    entry.name: minecraft_version(entry) for entry in versions
                },
                "minecraft_dir": str(minecraft_home(settings)),
                "worlds": list_worlds(game_home(settings)),
            },
            ensure_ascii=False,
        )
    )
    return 0


def play_from_config() -> int:
    """Start the game using launcher.ini, without opening the window."""
    settings = read_config()
    versions = horizon_versions()
    version = default_version(versions, settings)
    if not version:
        print("Horizon не установлен или ещё не подготовлен.", file=sys.stderr)
        return 1
    version_dir = VERSIONS_DIR / version
    if not is_prepared(version_dir):
        print("Версия не подготовлена: запусти лаунчер и нажми «Скачать».", file=sys.stderr)
        return 1
    home = game_home(settings)
    world = settings.get("world", "")
    if world and not (home / "saves" / world / "level.dat").is_file():
        world = ""
    try:
        launch_game(version_dir, world, settings.get("nick", ""), home)
    except OSError as error:
        print(f"Не удалось запустить Horizon: {error}", file=sys.stderr)
        return 1
    return 0


def download_from_config(tag: str = "") -> int:
    """Download Minecraft's files and prepare one version, printing progress."""
    settings = read_config()
    versions = horizon_versions()
    version = tag or default_version(versions, settings)
    if not version:
        print("Horizon не установлен: сначала запусти официальный лаунчер.", file=sys.stderr)
        return 1
    version_dir = VERSIONS_DIR / version
    if not version_dir.is_dir():
        print(f"Нет версии {version}.", file=sys.stderr)
        return 1

    def progress(done: int, total: int, text: str) -> None:
        print(f"[{done}/{total}] {text}", flush=True)

    def log(line: str) -> None:
        print(line, flush=True)

    try:
        prepare_version(version_dir, minecraft_home(settings), progress, log)
    except (DownloadError, OSError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1
    print("Готово.", flush=True)
    return 0


def main() -> int:
    if "--check" in sys.argv:
        return check()
    if "--play" in sys.argv:
        return play_from_config()
    if "--download" in sys.argv:
        tag = ""
        if "--version" in sys.argv:
            try:
                tag = sys.argv[sys.argv.index("--version") + 1]
            except IndexError:
                print("--version требует имя папки версии.", file=sys.stderr)
                return 2
        return download_from_config(tag)
    enable_dpi_awareness()
    Launcher().mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
