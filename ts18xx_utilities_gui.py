import codecs
import json
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path

#1.0.2

# 設定存在使用者家目錄(一定可寫入);舊版存在程式旁邊的檔案仍會讀取一次
SETTINGS_FILE = Path.home() / ".ts18xx_utilities_gui.json"
LEGACY_SETTINGS_FILE = Path(__file__).with_name("ts18xx_utilities_gui_settings.json")

# 固定不變的子命令(其餘固定參數寫在 build_cmd 內)
SUBCOMMAND = "program"

# 所有可勾選的組合,燒錄時依這個順序執行
COMBOS = [(fi, cs) for fi in (0, 1, 2) for cs in (0, 1)]

DEFAULTS = {
    "exe": "ts18xx_ftdi_flash",
    "bin_dir": "",
}

# TFLX Provisioning 分頁的預設路徑
TFLX_DEFAULT_EXE = r"C:\Users\jason\tpds\2.3.9_ts18xx_v100\Scripts\ts18xx_tflx_prov.exe"
TFLX_DEFAULT_IMG = r"C:\Users\jason\.trustplatform\ts18xx_tools_assets_1.0.0rc5\build\ap_otp.bin"


def _stream_env():
    # 讓工具(Python 程式)輸出 UTF-8,否則輸出到管道時會用 cp950,字元會亂碼
    return dict(os.environ, PYTHONUNBUFFERED="1",
                PYTHONIOENCODING="utf-8", PYTHONUTF8="1")


class TflxProvTab(ttk.Frame):
    """ts18xx_tflx_prov.exe 的三個動作:Start Provisioning / Stash Load + Exit / Stash Clear"""

    # action -> 顯示名稱
    LABELS = {
        "write": "Start Provisioning (TFLX STASH Write)",
        "load": "Stash Load + Exit",
        "clear": "Stash Clear",
    }

    def __init__(self, parent, saved, on_change):
        super().__init__(parent)
        self.on_change = on_change
        self.proc = None
        self.action = None
        self.out_q = queue.Queue()
        self.captured = []
        self._cr_pending = False

        def pick(key, default):
            v = saved.get(key)
            return v if isinstance(v, str) and v else default

        self.vars = {
            "exe": tk.StringVar(value=pick("tflx_exe", TFLX_DEFAULT_EXE)),
            "img": tk.StringVar(value=pick("tflx_img", TFLX_DEFAULT_IMG)),
        }
        self._build_ui()
        for v in self.vars.values():
            v.trace_add("write", lambda *_: (self._update_preview(), self.on_change()))
        self._update_preview()
        self.after(100, self._poll_output)

    # ---------- UI ----------
    def _build_ui(self):
        pad = {"padx": 6, "pady": 5}

        f = ttk.LabelFrame(self, text="路徑")
        f.pack(fill="x", padx=10, pady=(10, 4))
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="ts18xx_tflx_prov.exe").grid(row=0, column=0, sticky="e", **pad)
        ttk.Entry(f, textvariable=self.vars["exe"]).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(f, text="選擇…", command=self._browse_exe).grid(row=0, column=2, **pad)

        ttk.Label(f, text="ap_otp.bin").grid(row=1, column=0, sticky="e", **pad)
        ttk.Entry(f, textvariable=self.vars["img"]).grid(row=1, column=1, sticky="ew", **pad)
        ttk.Button(f, text="選擇…", command=self._browse_img).grid(row=1, column=2, **pad)

        self.img_status = ttk.Label(f, text="")
        self.img_status.grid(row=2, column=1, sticky="w", padx=6)

        ttk.Label(self, foreground="gray",
                  text="固定參數:--iface_type spi  -i 0(FTDI SPI, channel 0)").pack(
            anchor="w", padx=14, pady=(0, 2))
        ttk.Label(self, foreground="#c0392b",
                  text="執行任何動作前,請先按下 EVB 的 RESET,讓裝置進入 provisioning mode。").pack(
            anchor="w", padx=14, pady=(0, 2))

        # 指令預覽
        f_prev = ttk.LabelFrame(self, text="對應的指令")
        f_prev.pack(fill="x", padx=10, pady=4)
        f_prev.columnconfigure(0, weight=1)
        self.preview = tk.Text(f_prev, height=9, wrap="word", state="disabled",
                               background=self.winfo_toplevel().cget("background"), relief="flat")
        pys = ttk.Scrollbar(f_prev, orient="vertical", command=self.preview.yview)
        self.preview.configure(yscrollcommand=pys.set)
        self.preview.grid(row=0, column=0, sticky="ew", padx=(6, 0), pady=4)
        pys.grid(row=0, column=1, sticky="ns", pady=4)
        ttk.Button(f_prev, text="複製全部指令", command=self._copy_cmd).grid(
            row=1, column=0, columnspan=2, sticky="e", padx=6, pady=(0, 4))

        # 三個動作按鈕
        f_btn = ttk.Frame(self)
        f_btn.pack(fill="x", padx=10, pady=4)
        self.btn_write = ttk.Button(f_btn, text="⚡ Start Provisioning",
                                    command=lambda: self.run("write"))
        self.btn_write.pack(side="left")
        self.btn_load = ttk.Button(f_btn, text="↻ Stash Load + Exit",
                                   command=lambda: self.run("load"))
        self.btn_load.pack(side="left", padx=6)
        self.btn_clear = ttk.Button(f_btn, text="⌫ Stash Clear",
                                    command=lambda: self.run("clear"))
        self.btn_clear.pack(side="left")
        self.action_btns = [self.btn_write, self.btn_load, self.btn_clear]

        f_btn2 = ttk.Frame(self)
        f_btn2.pack(fill="x", padx=10, pady=(0, 4))
        self.btn_stop = ttk.Button(f_btn2, text="■ 停止", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left")
        ttk.Button(f_btn2, text="清除輸出", command=self._clear_log).pack(side="left", padx=6)
        self.status = tk.StringVar(value="就緒")
        ttk.Label(f_btn2, textvariable=self.status).pack(side="right")

        # 輸出
        f_log = ttk.LabelFrame(self, text="輸出")
        f_log.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        f_log.rowconfigure(0, weight=1)
        f_log.columnconfigure(0, weight=1)
        self.log = tk.Text(f_log, wrap="none", state="disabled", font=("Consolas", 10))
        ys = ttk.Scrollbar(f_log, orient="vertical", command=self.log.yview)
        xs = ttk.Scrollbar(f_log, orient="horizontal", command=self.log.xview)
        self.log.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        self.log.tag_configure("err", foreground="#c0392b")
        self.log.tag_configure("ok", foreground="#1e8449")
        self.log.tag_configure("info", foreground="#2471a3")

    # ---------- 指令組合 ----------
    def build_cmd(self, action):
        exe = self.vars["exe"].get().strip()
        base = [exe, "--iface_type", "spi", "-i", "0"]
        if action == "write":
            return base + ["write", "--img_in", self.vars["img"].get().strip(), "--stash"]
        if action == "load":
            return base + ["exit", "stash"]
        return base + ["cmd", "--stash_clear"]

    def cmdline(self, action):
        line = subprocess.list2cmdline(self.build_cmd(action))
        # 工具會詢問是否更新韌體 [y/N],GUI 會自動回答 n,命令列要用管線送入
        return f'echo n | {line}' if action == "write" else line

    def _update_preview(self):
        img = self.vars["img"].get().strip()
        if not img:
            self.img_status.configure(text="", foreground="gray")
        elif Path(img).is_file():
            self.img_status.configure(text="✔ 找到 ap_otp.bin", foreground="#1e8449")
        else:
            self.img_status.configure(text="✘ 找不到 ap_otp.bin", foreground="#c0392b")

        text = "\n\n".join(f"[{self.LABELS[a]}]\n{self.cmdline(a)}"
                           for a in ("write", "load", "clear"))
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", text)
        self.preview.configure(state="disabled")

    def _copy_cmd(self):
        self.clipboard_clear()
        self.clipboard_append("\n".join(self.cmdline(a) for a in ("write", "load", "clear")))
        self.status.set("已複製指令")

    # ---------- 檔案選擇 ----------
    def _browse_exe(self):
        init = os.path.dirname(self.vars["exe"].get()) or None
        if init and not os.path.isdir(init):
            init = None
        p = filedialog.askopenfilename(title="選擇 ts18xx_tflx_prov.exe", initialdir=init,
                                       filetypes=[("可執行檔", "*.exe *.bat *.cmd"), ("所有檔案", "*.*")])
        if p:
            self.vars["exe"].set(os.path.normpath(p))

    def _browse_img(self):
        init = os.path.dirname(self.vars["img"].get()) or None
        if init and not os.path.isdir(init):
            init = None
        p = filedialog.askopenfilename(title="選擇 ap_otp.bin", initialdir=init,
                                       filetypes=[("OTP binary", "*.bin"), ("所有檔案", "*.*")])
        if p:
            self.vars["img"].set(os.path.normpath(p))

    # ---------- 執行 ----------
    def _set_running(self, running):
        for b in self.action_btns:
            b.configure(state="disabled" if running else "normal")
        self.btn_stop.configure(state="normal" if running else "disabled")

    def run(self, action):
        if self.proc is not None:
            return
        if not self.vars["exe"].get().strip():
            messagebox.showwarning("缺少參數", "請先填入 ts18xx_tflx_prov.exe 的路徑。")
            return
        if action == "write":
            img = self.vars["img"].get().strip()
            if not img or not Path(img).is_file():
                messagebox.showerror("找不到檔案", f"找不到 ap_otp.bin:\n{img}")
                return

        cmd = self.build_cmd(action)
        self.action = action
        self.captured = []
        self._append(f"\n===== {self.LABELS[action]} =====\n", "info")
        self._append(f"> {self.cmdline(action)}\n", "info")
        self.status.set(f"執行中:{self.LABELS[action]}")
        self._set_running(True)

        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        try:
            self.proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE if action == "write" else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                bufsize=0, env=_stream_env(), creationflags=flags)
        except FileNotFoundError:
            self._append("找不到 ts18xx_tflx_prov.exe,請確認路徑。\n", "err")
            self._set_running(False)
            self.status.set("失敗:找不到執行檔")
            return
        except Exception as e:
            self._append(f"啟動失敗:{e}\n", "err")
            self._set_running(False)
            self.status.set("失敗")
            return

        if action == "write":
            # 等同 "n" | ...:自動回答「是否更新韌體」的 [y/N] 提示
            try:
                self.proc.stdin.write(b"n\n")
                self.proc.stdin.close()
            except OSError:
                pass

        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def _reader(self, proc):
        dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            text = dec.decode(chunk)
            if text:
                self.out_q.put(("data", text))
        proc.wait()
        self.out_q.put(("done", proc.returncode))

    def _poll_output(self):
        try:
            while True:
                kind, data = self.out_q.get_nowait()
                if kind == "data":
                    self.captured.append(data)
                    self._append_stream(data)
                else:
                    self._finish(data)
        except queue.Empty:
            pass
        self.after(100, self._poll_output)

    def _judge(self, action, code, text):
        """這個工具失敗時結束碼仍是 0,所以要看輸出文字判斷。"""
        if code != 0:
            return False
        if re.search(r"\[ERROR|Traceback", text):
            return False
        if action == "write":
            return "bytes match" in text and "left provisioning mode" in text
        if action == "clear":
            return "erased from on-device flash" in text
        # exit stash:要同時看到「還原 OTP 值」與「離開 provisioning mode」
        return "Restoring OTP values" in text and "left provisioning mode" in text

    def _finish(self, code):
        self.proc = None
        self._end_stream_line()
        text = "".join(self.captured)
        ok = self._judge(self.action, code, text)
        name = self.LABELS[self.action]
        if ok:
            self._append(f"[完成] {name}\n", "ok")
            self.status.set(f"完成:{name}")
        else:
            self._append(f"[失敗] {name}(結束碼 {code})\n", "err")
            if "RW_ERROR" in text:
                self._append("提示:請先按下 EVB 的 RESET 讓裝置進入 provisioning mode,再重試。\n", "err")
            self.status.set(f"失敗:{name}")
        self._set_running(False)

    def stop(self):
        if self.proc is not None:
            self.proc.terminate()
            self._append("[已要求停止]\n", "err")

    def terminate(self):
        if self.proc is not None:
            self.proc.terminate()

    # ---------- 輸出 ----------
    def _append(self, text, tag=None):
        self.log.configure(state="normal")
        self.log.insert("end", text, tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _append_stream(self, text):
        """\\r 代表回到行首,下一段文字覆蓋同一行。"""
        text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
        self.log.configure(state="normal")
        for tok in re.split(r"(\r\n|\r|\n)", text):
            if not tok:
                continue
            if tok == "\r":
                self._cr_pending = True
            elif tok in ("\n", "\r\n"):
                self.log.insert("end", "\n")
                self._cr_pending = False
            else:
                if self._cr_pending:
                    self.log.delete("end-1c linestart", "end-1c")
                    self._cr_pending = False
                self.log.insert("end", tok)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _end_stream_line(self):
        self._cr_pending = False
        self.log.configure(state="normal")
        if self.log.get("end-2c", "end-1c") not in ("\n", ""):
            self.log.insert("end", "\n")
        self.log.configure(state="disabled")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")


class FlashGui(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("TS18xx Utilities Tool")
        self.geometry("820x780")
        self.minsize(780, 640)

        self.proc = None
        self.out_q = queue.Queue()
        self._cr_pending = False  # 上次收到 \r,下一段文字要覆蓋最後一行

        # 依序燒錄的狀態
        self.jobs = []          # 尚未執行的 (flash_index, cs)
        self.job_total = 0
        self.job_results = []   # [((fi, cs), ok, code)]
        self.cur = None
        self.cancelled = False

        self._raw = {}
        self.vars = {k: tk.StringVar(value=v) for k, v in DEFAULTS.items()}
        self.sel = {c: tk.BooleanVar(value=(c == (0, 0))) for c in COMBOS}
        self._load_settings()

        # 分頁
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True)
        tab_flash = ttk.Frame(self.nb)
        self.nb.add(tab_flash, text="SPI Flash 燒錄")
        self._build_ui(tab_flash)
        self.tflx = TflxProvTab(self.nb, self._raw, self._save_settings)
        self.nb.add(self.tflx, text="TFLX Provisioning")

        for v in list(self.vars.values()) + list(self.sel.values()):
            v.trace_add("write", lambda *_: (self._update_preview(), self._save_settings()))
        self._update_preview()
        self.after(100, self._poll_output)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- UI ----------
    def _build_ui(self, root):
        pad = {"padx": 6, "pady": 5}

        f = ttk.LabelFrame(root, text="路徑")
        f.pack(fill="x", padx=10, pady=(10, 4))
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="ts18xx_ftdi_flash").grid(row=0, column=0, sticky="e", **pad)
        ttk.Entry(f, textvariable=self.vars["exe"]).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(f, text="選擇…", command=self._browse_exe).grid(row=0, column=2, **pad)

        ttk.Label(f, text="spi_image 資料夾").grid(row=1, column=0, sticky="e", **pad)
        ttk.Entry(f, textvariable=self.vars["bin_dir"]).grid(row=1, column=1, sticky="ew", **pad)
        ttk.Button(f, text="選擇…", command=self._browse_bin_dir).grid(row=1, column=2, **pad)

        # 六個 Checkbutton
        f_sel = ttk.LabelFrame(root, text="要燒錄的項目(勾選的會依序燒錄)")
        f_sel.pack(fill="x", padx=10, pady=4)
        f_sel.columnconfigure(1, weight=1, uniform="cs")
        f_sel.columnconfigure(2, weight=1, uniform="cs")
        self.chks = []
        self.combo_labels = {}
        # 左邊:--flash_index(每一列);右邊:--cs 0 / --cs 1(兩欄)
        for fi in (0, 1, 2):
            ttk.Label(f_sel, text=f"--flash_index {fi}", font=("", 10, "bold")).grid(
                row=fi, column=0, sticky="w", padx=(10, 14), pady=4)
            for cs in (0, 1):
                cell = ttk.Frame(f_sel)
                cell.grid(row=fi, column=1 + cs, sticky="w", padx=6, pady=3)
                chk = ttk.Checkbutton(cell, text=f"--cs {cs}", variable=self.sel[(fi, cs)])
                chk.pack(anchor="w")
                lab = ttk.Label(cell, text="")
                lab.pack(anchor="w", padx=(24, 0))
                self.chks.append(chk)
                self.combo_labels[(fi, cs)] = lab
        f_selbtn = ttk.Frame(f_sel)
        f_selbtn.grid(row=3, column=0, columnspan=3, sticky="w", padx=10, pady=(2, 6))
        self.btn_all = ttk.Button(f_selbtn, text="全選", command=self._select_found)
        self.btn_all.pack(side="left")
        self.btn_none = ttk.Button(f_selbtn, text="全不選", command=lambda: self._set_all(False))
        self.btn_none.pack(side="left", padx=(6, 0))

        ttk.Label(root, foreground="gray",
                  text="固定參數:--target ext  --speed 12  --spi_mode 0  --bus_width single  program"
                  ).pack(anchor="w", padx=14, pady=(0, 2))

        # 指令預覽
        f_prev = ttk.LabelFrame(root, text="將依序執行的指令")
        f_prev.pack(fill="x", padx=10, pady=4)
        f_prev.columnconfigure(0, weight=1)
        self.preview = tk.Text(f_prev, height=5, wrap="word", state="disabled",
                               background=self.cget("background"), relief="flat")
        pys = ttk.Scrollbar(f_prev, orient="vertical", command=self.preview.yview)
        self.preview.configure(yscrollcommand=pys.set)
        self.preview.grid(row=0, column=0, sticky="ew", padx=(6, 0), pady=4)
        pys.grid(row=0, column=1, sticky="ns", pady=4)
        ttk.Button(f_prev, text="複製指令", command=self._copy_cmd).grid(
            row=1, column=0, columnspan=2, sticky="e", padx=6, pady=(0, 4))

        # 按鈕
        f_btn = ttk.Frame(root)
        f_btn.pack(fill="x", padx=10, pady=4)
        self.btn_run = ttk.Button(f_btn, text="▶ 開始燒錄", command=self.run)
        self.btn_run.pack(side="left")
        self.btn_stop = ttk.Button(f_btn, text="■ 停止", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        ttk.Button(f_btn, text="清除輸出", command=self._clear_log).pack(side="left")
        self.status = tk.StringVar(value="就緒")
        ttk.Label(f_btn, textvariable=self.status).pack(side="right")

        # 輸出
        f_log = ttk.LabelFrame(root, text="輸出")
        f_log.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        f_log.rowconfigure(0, weight=1)
        f_log.columnconfigure(0, weight=1)
        self.log = tk.Text(f_log, wrap="none", state="disabled", font=("Consolas", 10))
        ys = ttk.Scrollbar(f_log, orient="vertical", command=self.log.yview)
        xs = ttk.Scrollbar(f_log, orient="horizontal", command=self.log.xview)
        self.log.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        self.log.tag_configure("err", foreground="#c0392b")
        self.log.tag_configure("ok", foreground="#1e8449")
        self.log.tag_configure("info", foreground="#2471a3")

    def _set_all(self, value):
        for v in self.sel.values():
            v.set(value)

    def _select_found(self):
        """全選:只勾選資料夾內找得到 .bin 檔的項目,找不到的取消勾選。"""
        if not self.vars["bin_dir"].get().strip():
            self.status.set("請先選擇 spi_image 資料夾")
            return
        n = 0
        for (fi, cs), var in self.sel.items():
            found = Path(self.bin_path(fi, cs)).is_file()
            var.set(found)
            n += found
        self.status.set(f"已勾選 {n} 個找到檔案的項目")

    def _set_running(self, running):
        self.btn_run.configure(state="disabled" if running else "normal")
        self.btn_stop.configure(state="normal" if running else "disabled")
        state = "disabled" if running else "normal"
        for w in self.chks + [self.btn_all, self.btn_none]:
            w.configure(state=state)

    # ---------- 指令組合 ----------
    def selected_combos(self):
        return [c for c in COMBOS if self.sel[c].get()]

    def bin_name(self, fi, cs):
        """檔名依 flash_index 與 cs 自動決定:spi_image_port_{flash_index}_comp_{cs}.bin"""
        return f"spi_image_port_{fi}_comp_{cs}.bin"

    def bin_path(self, fi, cs):
        d = self.vars["bin_dir"].get().strip()
        return str(Path(d) / self.bin_name(fi, cs)) if d else self.bin_name(fi, cs)

    def build_cmd(self, fi, cs):
        return [self.vars["exe"].get().strip(),
                "--target", "ext", "--flash_index", str(fi),
                "--speed", "12", "--spi_mode", "0", "--bus_width", "single",
                SUBCOMMAND, "--bin", self.bin_path(fi, cs), "--cs", str(cs)]

    def all_cmdlines(self):
        return [subprocess.list2cmdline(self.build_cmd(fi, cs)) for fi, cs in self.selected_combos()]

    def _update_bin_status(self):
        has_dir = bool(self.vars["bin_dir"].get().strip())
        for (fi, cs), lab in self.combo_labels.items():
            name = self.bin_name(fi, cs)
            checked = self.sel[(fi, cs)].get()
            if not has_dir:
                lab.configure(text=name, foreground="gray")
            elif Path(self.bin_path(fi, cs)).is_file():
                lab.configure(text=f"{name}  ✔ 找到", foreground="#1e8449")
            else:
                lab.configure(text=f"{name}  ✘ 找不到",
                              foreground="#c0392b" if checked else "gray")

    def _update_preview(self):
        self._update_bin_status()
        lines = self.all_cmdlines()
        text = "\n".join(f"[{i}] {c}" for i, c in enumerate(lines, 1)) if lines else "(尚未勾選任何項目)"
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", text)
        self.preview.configure(state="disabled")

    def _copy_cmd(self):
        self.clipboard_clear()
        self.clipboard_append("\n".join(self.all_cmdlines()))
        self.status.set("已複製指令")

    # ---------- 檔案選擇 ----------
    def _browse_exe(self):
        init = os.path.dirname(self.vars["exe"].get()) or None
        p = filedialog.askopenfilename(title="選擇 ts18xx_ftdi_flash", initialdir=init,
                                       filetypes=[("可執行檔", "*.exe *.bat *.cmd"), ("所有檔案", "*.*")])
        if p:
            self.vars["exe"].set(p)

    def _browse_bin_dir(self):
        init = self.vars["bin_dir"].get().strip() or None
        if init and not os.path.isdir(init):
            init = None
        p = filedialog.askdirectory(title="選擇 spi_image_port_x_comp_x.bin 所在資料夾", initialdir=init)
        if p:
            self.vars["bin_dir"].set(os.path.normpath(p))

    # ---------- 依序燒錄 ----------
    def run(self):
        if self.proc is not None or self.jobs:
            return
        combos = self.selected_combos()
        if not combos:
            messagebox.showwarning("尚未勾選", "請至少勾選一個要燒錄的項目。")
            return
        if not self.vars["bin_dir"].get().strip():
            messagebox.showwarning("缺少參數", "請先選擇 spi_image 所在的資料夾。")
            return
        missing = [self.bin_path(fi, cs) for fi, cs in combos if not Path(self.bin_path(fi, cs)).is_file()]
        if missing:
            messagebox.showerror("找不到檔案", "資料夾內缺少以下檔案,尚未開始燒錄:\n\n" + "\n".join(missing))
            return

        self.jobs = list(combos)
        self.job_total = len(combos)
        self.job_results = []
        self.cancelled = False
        self._set_running(True)
        self._next_job()

    def _next_job(self):
        fi, cs = self.jobs.pop(0)
        self.cur = (fi, cs)
        idx = self.job_total - len(self.jobs)
        cmd = self.build_cmd(fi, cs)
        self._append(f"\n===== [{idx}/{self.job_total}] flash_index {fi}, cs {cs} =====\n", "info")
        self._append(f"> {subprocess.list2cmdline(cmd)}\n", "info")
        self.status.set(f"燒錄中 ({idx}/{self.job_total}):flash {fi} / cs {cs}")

        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                bufsize=0, env=_stream_env(), creationflags=flags)
        except FileNotFoundError:
            self._append("找不到 ts18xx_ftdi_flash,請確認路徑或 PATH 設定。\n", "err")
            self.job_results.append(((fi, cs), False, None))
            self._all_done()
            return
        except Exception as e:
            self._append(f"啟動失敗:{e}\n", "err")
            self.job_results.append(((fi, cs), False, None))
            self._all_done()
            return

        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def _reader(self, proc):
        dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            text = dec.decode(chunk)
            if text:
                self.out_q.put(("data", text))
        proc.wait()
        self.out_q.put(("done", proc.returncode))

    def _poll_output(self):
        try:
            while True:
                kind, data = self.out_q.get_nowait()
                if kind == "data":
                    self._append_stream(data)
                else:
                    self._finish(data)
        except queue.Empty:
            pass
        self.after(100, self._poll_output)

    def _finish(self, code):
        """目前這一個項目結束:成功就接著燒下一個,失敗或被停止就中止後面的。"""
        self.proc = None
        self._end_stream_line()
        ok = (code == 0)
        self.job_results.append((self.cur, ok, code))
        if ok:
            self._append("[完成] 結束碼 0\n", "ok")
        else:
            self._append(f"[失敗] 結束碼 {code}\n", "err")
        if ok and not self.cancelled and self.jobs:
            self._next_job()
        else:
            self._all_done()

    def _all_done(self):
        self.proc = None
        self._set_running(False)
        skipped = list(self.jobs)
        self.jobs = []
        failed = [r for r in self.job_results if not r[1]]

        lines = ["", "===== 結果 ====="]
        for (fi, cs), ok, code in self.job_results:
            lines.append(f"✔ flash {fi} / cs {cs}" if ok
                         else f"✘ flash {fi} / cs {cs}(結束碼 {code})")
        for fi, cs in skipped:
            lines.append(f"- flash {fi} / cs {cs}(未執行)")
        all_ok = not failed and not skipped
        self._append("\n".join(lines) + "\n", "ok" if all_ok else "err")

        if all_ok:
            self.status.set(f"全部完成 ({len(self.job_results)}/{self.job_total})")
        elif self.cancelled:
            self.status.set("已停止")
        else:
            fi, cs = failed[0][0]
            self.status.set(f"失敗:flash {fi} / cs {cs}")

    def stop(self):
        self.cancelled = True
        if self.proc is not None:
            self.proc.terminate()
            self._append("[已要求停止,後面的項目不會執行]\n", "err")

    # ---------- 輸出 ----------
    def _append(self, text, tag=None):
        self.log.configure(state="normal")
        self.log.insert("end", text, tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _append_stream(self, text):
        """處理子程序輸出:\\r 代表回到行首,下一段文字覆蓋同一行(像 cmd 一樣)。"""
        text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)  # 移除 ANSI 控制碼
        self.log.configure(state="normal")
        for tok in re.split(r"(\r\n|\r|\n)", text):
            if not tok:
                continue
            if tok == "\r":
                self._cr_pending = True
            elif tok in ("\n", "\r\n"):
                self.log.insert("end", "\n")
                self._cr_pending = False
            else:
                if self._cr_pending:
                    self.log.delete("end-1c linestart", "end-1c")
                    self._cr_pending = False
                self.log.insert("end", tok)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _end_stream_line(self):
        self._cr_pending = False
        self.log.configure(state="normal")
        if self.log.get("end-2c", "end-1c") not in ("\n", ""):
            self.log.insert("end", "\n")
        self.log.configure(state="disabled")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    # ---------- 設定存取 ----------
    def _load_settings(self):
        data = None
        for path in (SETTINGS_FILE, LEGACY_SETTINGS_FILE):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                break
            except (OSError, json.JSONDecodeError):
                continue
        if not isinstance(data, dict):
            return
        self._raw = data
        # 舊版存的是完整 .bin 路徑,轉成資料夾
        if "bin_dir" not in data and data.get("bin"):
            data["bin_dir"] = os.path.dirname(data["bin"])
        for k in DEFAULTS:
            if isinstance(data.get(k), str):
                self.vars[k].set(data[k])

        # 勾選狀態;舊版只有單一 flash_index / cs,就轉成只勾那一個
        selected = data.get("selected")
        if selected is None and ("flash_index" in data or "cs" in data):
            selected = [f"{data.get('flash_index', '0')},{data.get('cs', '0')}"]
        if isinstance(selected, list):
            chosen = {str(s) for s in selected}
            for (fi, cs), var in self.sel.items():
                var.set(f"{fi},{cs}" in chosen)

    def _save_settings(self):
        data = {k: v.get() for k, v in self.vars.items()}
        data["selected"] = [f"{fi},{cs}" for (fi, cs) in COMBOS if self.sel[(fi, cs)].get()]
        tflx = getattr(self, "tflx", None)
        if tflx is not None:
            data["tflx_exe"] = tflx.vars["exe"].get()
            data["tflx_img"] = tflx.vars["img"].get()
        try:
            SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _on_close(self):
        busy = self.proc is not None or self.tflx.proc is not None
        if busy:
            if not messagebox.askyesno("確認", "仍有程序在執行,確定要離開嗎?"):
                return
            self.cancelled = True
            if self.proc is not None:
                self.proc.terminate()
            self.tflx.terminate()
        self._save_settings()
        self.destroy()


if __name__ == "__main__":
    FlashGui().mainloop()
