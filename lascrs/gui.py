"""Tkinter GUI for LAS CRS Doctor. Standard library only (plus the core dependencies)."""
from __future__ import annotations

import os
import queue
import threading
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import core
from .core import ConvertOptions, ERROR, WARN, INFO, OK

PRESETS = [
    ("Suggested (auto UTM from file)", None),
    ("WGS 84 / UTM 32N  (EPSG:32632)", 32632),
    ("WGS 84 / UTM 33N  (EPSG:32633)", 32633),
    ("RDN2008 / UTM 32N (EPSG:6707)  Italy, ETRF2000", 6707),
    ("RDN2008 / UTM 33N (EPSG:6708)  Italy, ETRF2000", 6708),
    ("ETRS89 / UTM 32N  (EPSG:25832)", 25832),
    ("ETRS89 / UTM 33N  (EPSG:25833)", 25833),
    ("Custom EPSG...", -1),
]
SOURCE_PRESETS = [
    ("Use CRS stored in file", None),
    ("WGS 84 geographic (EPSG:4326)", 4326),
    ("RDN2008 geographic (EPSG:6706)  coords really ETRF2000", 6706),
    ("ETRS89 geographic (EPSG:4258)", 4258),
    ("Custom EPSG...", -1),
]
ICON = {OK: "OK", INFO: "OK", WARN: "WARN", ERROR: "PROBLEM"}


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("LAS CRS Doctor")
        self.geometry("1050x760")
        self.minsize(820, 600)
        self.inspections: dict[str, core.Inspection] = {}
        self.q: queue.Queue = queue.Queue()
        self.cancel_evt = threading.Event()
        self.worker: threading.Thread | None = None
        self._build()
        self.after(100, self._poll)

    # ------------------------------------------------------------------ UI --
    def _build(self):
        pad = dict(padx=6, pady=4)

        top = ttk.Frame(self); top.pack(fill="x", **pad)
        ttk.Button(top, text="Add files...", command=self.add_files).pack(side="left")
        ttk.Button(top, text="Add folder...", command=self.add_folder).pack(side="left", padx=4)
        ttk.Button(top, text="Remove selected", command=self.remove_selected).pack(side="left")
        ttk.Button(top, text="Clear", command=self.clear).pack(side="left", padx=4)
        ttk.Button(top, text="Save report...", command=self.save_report).pack(side="right")

        pw = ttk.PanedWindow(self, orient="vertical"); pw.pack(fill="both", expand=True, **pad)

        cols = ("status", "points", "crs", "extent", "size")
        self.tree = ttk.Treeview(pw, columns=cols, show="tree headings", height=8)
        self.tree.heading("#0", text="File")
        for c, t, w in (("status", "Status", 80), ("points", "Points", 110), ("crs", "CRS", 260),
                        ("extent", "Approx. extent", 150), ("size", "Size", 80)):
            self.tree.heading(c, text=t); self.tree.column(c, width=w, anchor="w")
        self.tree.column("#0", width=280)
        self.tree.tag_configure(ERROR, background="#f8d7da")
        self.tree.tag_configure(WARN, background="#fff3cd")
        self.tree.tag_configure(OK, background="#d1e7dd")
        self.tree.tag_configure(INFO, background="#d1e7dd")
        self.tree.bind("<<TreeviewSelect>>", self.show_selected)
        pw.add(self.tree, weight=1)

        rep = ttk.Frame(pw)
        self.text = tk.Text(rep, wrap="word", font=("Consolas", 9) if os.name == "nt" else ("Monospace", 9))
        sb = ttk.Scrollbar(rep, command=self.text.yview); self.text.configure(yscrollcommand=sb.set)
        self.text.pack(side="left", fill="both", expand=True); sb.pack(side="right", fill="y")
        for lvl, col in ((ERROR, "#b02a37"), (WARN, "#997404"), (OK, "#146c43"), (INFO, "#055160")):
            self.text.tag_configure(lvl, foreground=col, font=("TkDefaultFont", 9, "bold"))
        pw.add(rep, weight=2)

        conv = ttk.LabelFrame(self, text="Convert"); conv.pack(fill="x", **pad)
        g = dict(sticky="w", padx=4, pady=3)

        ttk.Label(conv, text="Source CRS:").grid(row=0, column=0, **g)
        self.src_var = tk.StringVar(value=SOURCE_PRESETS[0][0])
        cb = ttk.Combobox(conv, textvariable=self.src_var, values=[p[0] for p in SOURCE_PRESETS],
                          state="readonly", width=52)
        cb.grid(row=0, column=1, **g)
        ttk.Label(conv, text="custom EPSG:").grid(row=0, column=2, **g)
        self.src_custom = ttk.Entry(conv, width=10); self.src_custom.grid(row=0, column=3, **g)

        ttk.Label(conv, text="Target CRS:").grid(row=1, column=0, **g)
        self.dst_var = tk.StringVar(value=PRESETS[0][0])
        cb2 = ttk.Combobox(conv, textvariable=self.dst_var, values=[p[0] for p in PRESETS],
                           state="readonly", width=52)
        cb2.grid(row=1, column=1, **g)
        ttk.Label(conv, text="custom EPSG:").grid(row=1, column=2, **g)
        self.dst_custom = ttk.Entry(conv, width=10); self.dst_custom.grid(row=1, column=3, **g)

        ttk.Label(conv, text="Output:").grid(row=2, column=0, **g)
        of = ttk.Frame(conv); of.grid(row=2, column=1, columnspan=3, **g)
        self.fmt_var = tk.StringVar(value="laz")
        ttk.Radiobutton(of, text="LAZ (compressed, ~4x smaller)", variable=self.fmt_var, value="laz").pack(side="left")
        ttk.Radiobutton(of, text="LAS", variable=self.fmt_var, value="las").pack(side="left", padx=8)
        self.merge_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(of, text="Merge all files into one", variable=self.merge_var).pack(side="left", padx=12)
        ttk.Label(of, text="Precision (m):").pack(side="left", padx=(12, 2))
        self.scale_var = tk.StringVar(value="0.001")
        ttk.Combobox(of, textvariable=self.scale_var, values=["0.0001", "0.001", "0.01"],
                     width=7, state="readonly").pack(side="left")

        ttk.Label(conv, text="Output folder:").grid(row=3, column=0, **g)
        self.out_dir = tk.StringVar()
        ttk.Entry(conv, textvariable=self.out_dir, width=55).grid(row=3, column=1, **g)
        ttk.Button(conv, text="Browse...", command=self.pick_out).grid(row=3, column=2, **g)
        ttk.Label(conv, text="Merged name:").grid(row=4, column=0, **g)
        self.merge_name = tk.StringVar(value="merged_reprojected")
        ttk.Entry(conv, textvariable=self.merge_name, width=30).grid(row=4, column=1, **g)

        bf = ttk.Frame(self); bf.pack(fill="x", **pad)
        self.btn_run = ttk.Button(bf, text="Check and convert", command=self.run_convert)
        self.btn_run.pack(side="left")
        self.btn_cancel = ttk.Button(bf, text="Cancel", command=self.cancel_evt.set, state="disabled")
        self.btn_cancel.pack(side="left", padx=4)
        self.pbar = ttk.Progressbar(bf, maximum=1000); self.pbar.pack(side="left", fill="x", expand=True, padx=6)
        self.status = tk.StringVar(value="Add LAS/LAZ files to inspect them.")
        ttk.Label(self, textvariable=self.status, anchor="w").pack(fill="x", padx=8, pady=(0, 6))

    # ------------------------------------------------------------- files --
    def add_files(self):
        paths = filedialog.askopenfilenames(filetypes=[("LAS/LAZ", "*.las *.laz *.LAS *.LAZ"), ("All", "*.*")])
        self._inspect_async(list(paths))

    def add_folder(self):
        d = filedialog.askdirectory()
        if d:
            paths = [os.path.join(d, f) for f in sorted(os.listdir(d)) if f.lower().endswith((".las", ".laz"))]
            self._inspect_async(paths)

    def remove_selected(self):
        for iid in self.tree.selection():
            self.inspections.pop(iid, None); self.tree.delete(iid)

    def clear(self):
        self.inspections.clear(); self.tree.delete(*self.tree.get_children()); self.text.delete("1.0", "end")

    def pick_out(self):
        d = filedialog.askdirectory()
        if d:
            self.out_dir.set(d)

    def _inspect_async(self, paths):
        paths = [p for p in paths if p not in self.inspections]
        if not paths:
            return
        if not self.out_dir.get():
            self.out_dir.set(os.path.dirname(paths[0]))

        def job():
            for i, p in enumerate(paths, 1):
                self.q.put(("status", f"Inspecting {os.path.basename(p)} ({i}/{len(paths)})..."))
                try:
                    self.q.put(("inspected", core.inspect(p)))
                except Exception as e:
                    self.q.put(("error", f"{p}: {e}"))
            self.q.put(("status", "Inspection done. Select a file to read its report."))
        threading.Thread(target=job, daemon=True).start()

    def _add_row(self, ins: core.Inspection):
        self.inspections[ins.path] = ins
        crs = ins.crs_name + (f" (EPSG:{ins.crs_epsg})" if ins.crs_epsg else "")
        ext = f"{ins.metric_span[0]:.0f} x {ins.metric_span[1]:.0f} m"
        self.tree.insert("", "end", iid=ins.path, text=os.path.basename(ins.path), tags=(ins.worst,),
                         values=(ICON[ins.worst], f"{ins.point_count:,}", crs, ext,
                                 f"{ins.file_size / 1e9:.2f} GB"))
        if len(self.inspections) == 1:
            self.tree.selection_set(ins.path)

    def show_selected(self, *_):
        sel = self.tree.selection()
        if not sel:
            return
        self._write_report([self.inspections[s] for s in sel])

    def _write_report(self, items):
        self.text.delete("1.0", "end")
        for ins in items:
            for line in ins.report().splitlines():
                tag = next((lvl for lvl in (ERROR, WARN, OK, INFO) if line.startswith(f"[{lvl}]")), None)
                self.text.insert("end", line + "\n", tag)
            self.text.insert("end", "\n" + "-" * 90 + "\n\n")

    def save_report(self):
        if not self.inspections:
            return
        p = filedialog.asksaveasfilename(defaultextension=".txt", filetypes=[("Text", "*.txt")])
        if p:
            with open(p, "w", encoding="utf-8") as f:
                f.write("\n\n".join(i.report() for i in self.inspections.values()))
            self.status.set(f"Report saved to {p}")

    # ----------------------------------------------------------- convert --
    def _epsg_from(self, var, presets, custom_entry, auto_value=None):
        label = var.get()
        val = dict(presets)[label]
        if val == -1:
            txt = custom_entry.get().strip().upper().replace("EPSG:", "")
            if not txt.isdigit():
                raise ValueError(f"Enter a numeric custom EPSG code for '{label}'.")
            val = int(txt)
            core.CRS.from_epsg(val)  # validates
        return auto_value if val is None else val

    def run_convert(self):
        sel = self.tree.selection() or tuple(self.inspections)
        items = [self.inspections[s] for s in sel]
        if not items:
            messagebox.showinfo("LAS CRS Doctor", "Add at least one file first."); return
        try:
            src = self._epsg_from(self.src_var, SOURCE_PRESETS, self.src_custom)
            suggested = {i.suggested_epsg for i in items if i.suggested_epsg}
            auto = suggested.pop() if len(suggested) == 1 else None
            dst = self._epsg_from(self.dst_var, PRESETS, self.dst_custom, auto_value=auto)
            if dst is None:
                raise ValueError("No automatic target could be derived (file already projected, or files in "
                                 "different UTM zones). Choose a target CRS explicitly.")
            scale = float(self.scale_var.get())
        except Exception as e:
            messagebox.showerror("Invalid settings", str(e)); return

        out_dir = self.out_dir.get()
        if not out_dir or not os.path.isdir(out_dir):
            messagebox.showerror("Output folder", "Choose an existing output folder."); return
        ext = ".laz" if self.fmt_var.get() == "laz" else ".las"
        opts = ConvertOptions(target_epsg=dst, source_epsg=src, compress=ext == ".laz",
                              scale_xy=scale, scale_z=scale)

        # Build jobs: list of (inputs, output)
        if self.merge_var.get() and len(items) > 1:
            jobs = [(items, os.path.join(out_dir, self.merge_name.get().strip() + ext))]
        else:
            jobs = [([i], os.path.join(out_dir, os.path.splitext(os.path.basename(i.path))[0]
                                       + f"_epsg{dst}{ext}")) for i in items]

        # Already-projected inputs: ask before reprojecting
        already = [os.path.basename(i.path) for i in items if i.crs_kind == "projected" and not src]
        msgs = []
        if already:
            msgs.append(f"[INFO] Already in a projected CRS: {', '.join(already)}. "
                        "They will still be reprojected to the target.")
        blocking = False
        for inputs, out in jobs:
            for f in core.preflight(inputs, out, opts):
                msgs.append(f"{os.path.basename(out)}: {f}")
                blocking |= f.level == ERROR
        summary = (f"Target: EPSG:{dst} ({core.epsg_name(dst)})\n"
                   f"Source: {'from file' if not src else f'EPSG:{src} ({core.epsg_name(src)})'}\n"
                   f"Jobs: {len(jobs)}   Format: {ext}   Precision: {scale} m\n\n" + "\n".join(msgs))
        if blocking:
            messagebox.showerror("Cannot convert", summary); return
        if not messagebox.askyesno("Confirm conversion", summary + "\n\nProceed?"):
            return

        self.cancel_evt.clear()
        self.btn_run.configure(state="disabled"); self.btn_cancel.configure(state="normal")

        def job():
            results = []
            try:
                for k, (inputs, out) in enumerate(jobs, 1):
                    def cb(done, total, msg, k=k):
                        self.q.put(("progress", done / max(total, 1), f"[{k}/{len(jobs)}] {msg}"))
                    findings = core.convert(inputs, out, opts, progress=cb, cancel=self.cancel_evt)
                    results.append((out, findings))
                self.q.put(("done", results))
            except core.Cancelled:
                self.q.put(("cancelled", jobs))
            except Exception as e:
                self.q.put(("failed", f"{e}\n\n{traceback.format_exc()}"))
        self.worker = threading.Thread(target=job, daemon=True); self.worker.start()

    # -------------------------------------------------------------- queue --
    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "status":
                    self.status.set(msg[1])
                elif kind == "inspected":
                    self._add_row(msg[1])
                elif kind == "error":
                    self.status.set(msg[1]); messagebox.showerror("Inspection failed", msg[1])
                elif kind == "progress":
                    self.pbar["value"] = msg[1] * 1000; self.status.set(msg[2])
                elif kind in ("done", "cancelled", "failed"):
                    self.btn_run.configure(state="normal"); self.btn_cancel.configure(state="disabled")
                    if kind == "done":
                        txt = "\n\n".join(os.path.basename(o) + "\n" + "\n".join(str(f) for f in fs)
                                          for o, fs in msg[1])
                        self.text.delete("1.0", "end"); self.text.insert("end", "CONVERSION RESULTS\n\n" + txt)
                        self.status.set("Conversion finished.")
                        bad = any(f.level == ERROR for _, fs in msg[1] for f in fs)
                        (messagebox.showwarning if bad else messagebox.showinfo)("Finished", txt)
                    elif kind == "cancelled":
                        self.status.set("Cancelled. The partial output file is incomplete: delete it.")
                        self.pbar["value"] = 0
                    else:
                        self.status.set("Conversion failed."); messagebox.showerror("Conversion failed", msg[1])
        except queue.Empty:
            pass
        self.after(100, self._poll)


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
