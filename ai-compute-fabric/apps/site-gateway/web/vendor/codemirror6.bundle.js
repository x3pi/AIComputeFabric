/**
 * AI Compute Fabric — Self-Contained Browser Code Editor Bundle (ADR-004, CodeMirror 6 Architecture)
 * Zero external CDN dependencies; 100% compliant with strict Content-Security-Policy (script-src 'self').
 * Provides:
 *   - Python + AICF SDK/CUDA syntax highlighting
 *   - Line numbers gutter + active line + per-line lint markers (error/warning)
 *   - Smart indentation (Tab = 4 spaces, Shift+Tab unindent, auto-indent after ':')
 *   - Auto-close brackets/quotes & bracket matching
 *   - Comment toggle (Ctrl+/), Search & Replace bar (Ctrl+F), Quick Test Run (Ctrl+Enter)
 *   - Autocomplete popup (Ctrl+Space or @app. / aicf_gpu.)
 *   - Multi-file tab management (user_app.py + helper .py modules)
 *   - Unified line-by-line Version Diff computation
 *   - Seamless two-way synchronization & graceful fallback with <textarea id="sl-code-editor">
 */
(function (global) {
  "use strict";

  const PY_KEYWORDS = new Set([
    "def", "class", "return", "import", "from", "as", "if", "elif", "else",
    "for", "while", "break", "continue", "pass", "try", "except", "finally",
    "raise", "with", "yield", "lambda", "and", "or", "not", "in", "is",
    "True", "False", "None", "async", "await", "global", "nonlocal", "assert", "del"
  ]);

  const PY_BUILTINS = new Set([
    "dict", "list", "set", "tuple", "str", "int", "float", "bool", "bytes",
    "len", "range", "enumerate", "zip", "map", "filter", "sorted", "sum",
    "min", "max", "abs", "round", "print", "isinstance", "getattr", "setattr",
    "hasattr", "Exception", "ValueError", "RuntimeError", "TypeError", "KeyError"
  ]);

  const AICF_COMPLETIONS = [
    { label: "aicf_gpu.device_info()", detail: "Thông tin GPU & VRAM thực tế", insert: "aicf_gpu.device_info()" },
    { label: "aicf_gpu.matmul(A, B)", detail: "Nhân ma trận 2D trên CUDA PTX", insert: "aicf_gpu.matmul(A, B)" },
    { label: "aicf_gpu.saxpy(alpha, x, y)", detail: "Vector Y = alpha*X + Y trên GPU", insert: "aicf_gpu.saxpy(2.5, x, y)" },
    { label: "aicf_gpu.cosine_search(q, docs, labels)", detail: "Tìm kiếm tương đồng Cosine trên GPU", insert: "aicf_gpu.cosine_search(query, docs, labels)" },
    { label: "@app.function(gpu=\"any\", vram_mb=1024)", detail: "Định nghĩa hàm Serverless GPU", insert: "@app.function(gpu=\"any\", vram_mb=1024)\ndef compute(payload: dict) -> dict:\n    return {\"ok\": True}" },
    { label: "@app.web_endpoint()", detail: "Định nghĩa HTTP JSON Web Endpoint", insert: "@app.web_endpoint()\ndef endpoint(payload: dict) -> dict:\n    return {\"ok\": True}" },
    { label: "@aicf.endpoint(gpu=\"RTX\", vram_mb=1024)", detail: "Decorator kiểu Beam/Beta9", insert: "@aicf.endpoint(gpu=\"RTX\", vram_mb=1024, keep_warm_seconds=120)" },
    { label: "aicf.Volume(\"shared-data\")", detail: "Gắn ổ đĩa bền vững /mnt/volumes", insert: "aicf.Volume(\"shared-data\")" },
    { label: "aicf.Secret(\"my-secret\")", detail: "Tiêm biến môi trường đã mã hóa", insert: "aicf.Secret(\"my-secret\")" }
  ];

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function highlightPythonLine(line) {
    if (!line) return "\u200b";
    let out = "";
    let i = 0;
    const n = line.length;
    while (i < n) {
      const ch = line[i];
      // Comment
      if (ch === "#") {
        out += '<span class="cm-tok-comment">' + escapeHtml(line.slice(i)) + "</span>";
        break;
      }
      // Strings
      if (ch === '"' || ch === "'") {
        const quote = line.slice(i, i + 3) === ch.repeat(3) ? ch.repeat(3) : ch;
        let j = i + quote.length;
        while (j < n) {
          if (line[j] === "\\") {
            j += 2;
            continue;
          }
          if (line.slice(j, j + quote.length) === quote) {
            j += quote.length;
            break;
          }
          j++;
        }
        out += '<span class="cm-tok-string">' + escapeHtml(line.slice(i, j)) + "</span>";
        i = j;
        continue;
      }
      // Decorator
      if (ch === "@" && (i === 0 || /\s/.test(line[i - 1]))) {
        let j = i + 1;
        while (j < n && /[A-Za-z0-9_.]/.test(line[j])) j++;
        out += '<span class="cm-tok-deco">' + escapeHtml(line.slice(i, j)) + "</span>";
        i = j;
        continue;
      }
      // Numbers
      if (/[0-9]/.test(ch) && (i === 0 || !/[A-Za-z_]/.test(line[i - 1]))) {
        let j = i + 1;
        while (j < n && /[0-9._xXa-fA-FeE+-]/.test(line[j])) j++;
        out += '<span class="cm-tok-num">' + escapeHtml(line.slice(i, j)) + "</span>";
        i = j;
        continue;
      }
      // Identifiers / Keywords
      if (/[A-Za-z_]/.test(ch)) {
        let j = i + 1;
        while (j < n && /[A-Za-z0-9_]/.test(line[j])) j++;
        const word = line.slice(i, j);
        if (PY_KEYWORDS.has(word)) {
          out += '<span class="cm-tok-kw">' + escapeHtml(word) + "</span>";
        } else if (word === "aicf_gpu" || word === "aicf") {
          out += '<span class="cm-tok-gpu">' + escapeHtml(word) + "</span>";
        } else if (PY_BUILTINS.has(word)) {
          out += '<span class="cm-tok-builtin">' + escapeHtml(word) + "</span>";
        } else if (line[j] === "(") {
          out += '<span class="cm-tok-fn">' + escapeHtml(word) + "</span>";
        } else {
          out += escapeHtml(word);
        }
        i = j;
        continue;
      }
      out += escapeHtml(ch);
      i++;
    }
    return out;
  }

  /**
   * Compute a unified line-by-line diff between oldText and newText (LCS algorithm).
   */
  function computeUnifiedDiff(oldText, newText, oldLabel = "deployed", newLabel = "editor") {
    const a = String(oldText || "").replace(/\r\n/g, "\n").split("\n");
    const b = String(newText || "").replace(/\r\n/g, "\n").split("\n");
    const n = a.length;
    const m = b.length;
    const dp = Array.from({ length: n + 1 }, () => new Int32Array(m + 1));
    for (let i = n - 1; i >= 0; i--) {
      for (let j = m - 1; j >= 0; j--) {
        if (a[i] === b[j]) dp[i][j] = 1 + dp[i + 1][j + 1];
        else dp[i][j] = Math.max(dp[i + 1][j], dp[i][j + 1]);
      }
    }
    const lines = [
      { type: "hdr", text: `--- ${oldLabel} (${n} dòng)` },
      { type: "hdr", text: `+++ ${newLabel} (${m} dòng)` }
    ];
    let i = 0, j = 0;
    let added = 0, removed = 0;
    while (i < n && j < m) {
      if (a[i] === b[j]) {
        lines.push({ type: "ctx", oldLine: i + 1, newLine: j + 1, text: "  " + a[i] });
        i++; j++;
      } else if (dp[i + 1][j] >= dp[i][j + 1]) {
        lines.push({ type: "del", oldLine: i + 1, newLine: null, text: "- " + a[i] });
        removed++; i++;
      } else {
        lines.push({ type: "add", oldLine: null, newLine: j + 1, text: "+ " + b[j] });
        added++; j++;
      }
    }
    while (i < n) {
      lines.push({ type: "del", oldLine: i + 1, newLine: null, text: "- " + a[i] });
      removed++; i++;
    }
    while (j < m) {
      lines.push({ type: "add", oldLine: null, newLine: j + 1, text: "+ " + b[j] });
      added++; j++;
    }
    return { lines, added, removed, identical: added === 0 && removed === 0 };
  }

  class AicfEditor {
    constructor(textarea, options = {}) {
      this.textarea = textarea;
      this.options = options;
      this.activeFile = options.mainFile || "user_app.py";
      this.files = { [this.activeFile]: textarea.value || "" };
      this.diagnostics = [];
      this.activeLine = 1;
      this._suppressSync = false;
      this._escTabArmed = false;
      this._history = [{ value: textarea.value || "", selStart: 0, selEnd: 0 }];
      this._historyIdx = 0;
      this._mount();
    }

    _pushHistory() {
      const ta = this.textarea;
      const cur = this._history[this._historyIdx];
      if (cur && cur.value === ta.value) return;
      this._history = this._history.slice(0, this._historyIdx + 1);
      this._history.push({ value: ta.value, selStart: ta.selectionStart || 0, selEnd: ta.selectionEnd || 0 });
      if (this._history.length > 100) this._history.shift();
      this._historyIdx = this._history.length - 1;
    }

    undo() {
      if (this._historyIdx <= 0) return false;
      this._historyIdx--;
      const snap = this._history[this._historyIdx];
      this.setValue(snap.value);
      this.textarea.setSelectionRange(snap.selStart, snap.selEnd);
      if (typeof this.options.onChange === "function") {
        this.options.onChange(this.getValue(), this.activeFile, this.getAllFiles());
      }
      return true;
    }

    redo() {
      if (this._historyIdx >= this._history.length - 1) return false;
      this._historyIdx++;
      const snap = this._history[this._historyIdx];
      this.setValue(snap.value);
      this.textarea.setSelectionRange(snap.selStart, snap.selEnd);
      if (typeof this.options.onChange === "function") {
        this.options.onChange(this.getValue(), this.activeFile, this.getAllFiles());
      }
      return true;
    }

    jumpToLine(line, col = 1) {
      const ta = this.textarea;
      const lines = (ta.value || "").split("\n");
      const targetLine = Math.max(1, Math.min(lines.length, parseInt(line, 10) || 1));
      let offset = 0;
      for (let i = 0; i < targetLine - 1; i++) {
        offset += lines[i].length + 1;
      }
      const lineLen = lines[targetLine - 1] ? lines[targetLine - 1].length : 0;
      const targetCol = Math.max(1, Math.min(lineLen + 1, parseInt(col, 10) || 1));
      const pos = offset + (targetCol - 1);
      ta.focus();
      ta.setSelectionRange(pos, pos);
      this.activeLine = targetLine;
      this._renderGutter();
    }

    _mount() {
      const ta = this.textarea;
      const wrap = document.createElement("div");
      wrap.className = "cm6-editor-wrap";
      wrap.id = "sl-cm6-wrap";

      // Gutter (line numbers + lint markers)
      const gutter = document.createElement("div");
      gutter.className = "cm6-gutter";
      gutter.id = "sl-cm6-gutter";
      gutter.setAttribute("aria-hidden", "true");

      // Editor stage (backdrop syntax highlight + transparent synced textarea)
      const stage = document.createElement("div");
      stage.className = "cm6-stage";

      const highlightPre = document.createElement("pre");
      highlightPre.className = "cm6-highlight-layer";
      highlightPre.id = "sl-cm6-highlight";
      highlightPre.setAttribute("aria-hidden", "true");

      ta.parentNode.insertBefore(wrap, ta);
      stage.appendChild(highlightPre);
      stage.appendChild(ta);
      wrap.appendChild(gutter);
      wrap.appendChild(stage);
      ta.classList.add("cm6-input-layer");

      // Floating Autocomplete menu
      const acPopup = document.createElement("div");
      acPopup.className = "cm6-autocomplete";
      acPopup.id = "sl-cm6-autocomplete";
      acPopup.style.display = "none";
      wrap.appendChild(acPopup);

      // Search & Replace bar (Ctrl+F)
      const findBar = document.createElement("div");
      findBar.className = "cm6-find-bar";
      findBar.id = "sl-cm6-find-bar";
      findBar.style.display = "none";
      findBar.innerHTML =
        '<input id="sl-cm6-find-input" class="mono" placeholder="Tìm kiếm (Ctrl+F)…" style="width:160px;padding:3px 7px;font-size:12px">' +
        '<input id="sl-cm6-replace-input" class="mono" placeholder="Thay thế bằng…" style="width:150px;padding:3px 7px;font-size:12px">' +
        '<button type="button" id="sl-cm6-btn-find-next" class="sm">Tìm tiếp</button>' +
        '<button type="button" id="sl-cm6-btn-replace" class="sm">Thay thế</button>' +
        '<button type="button" id="sl-cm6-btn-replace-all" class="sm">Thay tất cả</button>' +
        '<span id="sl-cm6-find-status" class="mut mono" style="font-size:11px"></span>' +
        '<button type="button" id="sl-cm6-btn-find-close" class="sm">✕</button>';
      wrap.parentNode.insertBefore(findBar, wrap);

      this.wrap = wrap;
      this.gutter = gutter;
      this.highlightPre = highlightPre;
      this.acPopup = acPopup;
      this.findBar = findBar;

      this._bindEvents();
      this.render();
    }

    _bindEvents() {
      const ta = this.textarea;

      const onInput = () => {
        this.files[this.activeFile] = ta.value;
        this._pushHistory();
        this._updateActiveLine();
        this.render();
        if (typeof this.options.onChange === "function") {
          this.options.onChange(this.getValue(), this.activeFile, this.getAllFiles());
        }
      };

      ta.addEventListener("input", onInput);
      ta.addEventListener("click", () => { this._updateActiveLine(); this._renderGutter(); this.hideAutocomplete(); });
      ta.addEventListener("keyup", e => {
        if (["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) {
          this._updateActiveLine();
          this._renderGutter();
        }
      });
      ta.addEventListener("scroll", () => {
        this.highlightPre.scrollTop = ta.scrollTop;
        this.highlightPre.scrollLeft = ta.scrollLeft;
        this.gutter.scrollTop = ta.scrollTop;
      });

      ta.addEventListener("keydown", e => this._handleKeyDown(e));

      // Intercept programmatic `.value = ...` assignments (e.g., Playwright `page.fill("#sl-code-editor", ...)`)
      const proto = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value");
      if (proto && proto.get && proto.set) {
        const self = this;
        Object.defineProperty(ta, "value", {
          get() {
            return proto.get.call(this);
          },
          set(v) {
            proto.set.call(this, v);
            if (!self._suppressSync) {
              self.files[self.activeFile] = String(v);
              self._pushHistory();
              self.render();
            }
          }
        });
      }

      // Bind Find/Replace bar events
      const findIn = this.findBar.querySelector("#sl-cm6-find-input");
      const repIn = this.findBar.querySelector("#sl-cm6-replace-input");
      const statusEl = this.findBar.querySelector("#sl-cm6-find-status");
      this.findBar.querySelector("#sl-cm6-btn-find-next").addEventListener("click", () => {
        const q = findIn.value;
        if (!q) return;
        const text = ta.value;
        const start = ta.selectionEnd || 0;
        let idx = text.indexOf(q, start);
        if (idx < 0) idx = text.indexOf(q, 0);
        if (idx >= 0) {
          ta.focus();
          ta.setSelectionRange(idx, idx + q.length);
          statusEl.textContent = `Vị trí ${idx}`;
        } else {
          statusEl.textContent = "Không tìm thấy";
        }
      });
      this.findBar.querySelector("#sl-cm6-btn-replace").addEventListener("click", () => {
        const q = findIn.value;
        if (!q) return;
        const sel = ta.value.slice(ta.selectionStart, ta.selectionEnd);
        if (sel === q) {
          ta.setRangeText(repIn.value, ta.selectionStart, ta.selectionEnd, "end");
          onInput();
        }
      });
      this.findBar.querySelector("#sl-cm6-btn-replace-all").addEventListener("click", () => {
        const q = findIn.value;
        if (!q) return;
        const parts = ta.value.split(q);
        const count = parts.length - 1;
        if (count > 0) {
          ta.value = parts.join(repIn.value);
          onInput();
          statusEl.textContent = `Đã thay ${count} vị trí`;
        }
      });
      this.findBar.querySelector("#sl-cm6-btn-find-close").addEventListener("click", () => {
        this.findBar.style.display = "none";
        ta.focus();
      });
    }

    _handleKeyDown(e) {
      const ta = this.textarea;
      const start = ta.selectionStart;
      const end = ta.selectionEnd;
      const val = ta.value;

      // Ctrl+Enter / Cmd+Enter -> Quick Test Run
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
        e.preventDefault();
        if (typeof this.options.onTestRun === "function") {
          this.options.onTestRun(this.getValue(), this.activeFile, this.getAllFiles());
        }
        return;
      }

      // Ctrl+F / Cmd+F -> Toggle Search & Replace bar
      if ((e.ctrlKey || e.metaKey) && (e.key === "f" || e.key === "F")) {
        e.preventDefault();
        this.toggleFindBar();
        return;
      }

      // Ctrl+Space -> Show Autocomplete
      if (e.ctrlKey && e.code === "Space") {
        e.preventDefault();
        this.showAutocomplete();
        return;
      }

      // Ctrl+Z / Cmd+Z -> Undo, Ctrl+Y / Ctrl+Shift+Z -> Redo
      if ((e.ctrlKey || e.metaKey) && !e.shiftKey && (e.key === "z" || e.key === "Z")) {
        e.preventDefault();
        this.undo();
        return;
      }
      if ((e.ctrlKey || e.metaKey) && ((e.shiftKey && (e.key === "z" || e.key === "Z")) || e.key === "y" || e.key === "Y")) {
        e.preventDefault();
        this.redo();
        return;
      }

      // Escape -> Close Autocomplete / Find bar or arm Esc+Tab focus escape (WCAG 2.1.2 No Keyboard Trap)
      if (e.key === "Escape") {
        this._escTabArmed = true;
        if (this.acPopup.style.display !== "none") {
          e.preventDefault();
          this.hideAutocomplete();
          return;
        }
        if (this.findBar.style.display !== "none") {
          e.preventDefault();
          this.findBar.style.display = "none";
          return;
        }
        return;
      }

      // Ctrl+/ or Cmd+/ -> Toggle line comment (# )
      if ((e.ctrlKey || e.metaKey) && e.key === "/") {
        this._escTabArmed = false;
        e.preventDefault();
        this.toggleComment();
        return;
      }

      // Tab / Shift+Tab -> 4-space indent / unindent (unless Esc+Tab was armed to escape focus)
      if (e.key === "Tab") {
        if (this._escTabArmed) {
          this._escTabArmed = false;
          const nextBtn = document.getElementById("sl-btn-deploy-code") || document.getElementById("sl-btn-lint-code");
          if (nextBtn) {
            e.preventDefault();
            nextBtn.focus();
          }
          return;
        }
        e.preventDefault();
        if (e.shiftKey) {
          const lineStart = val.lastIndexOf("\n", start - 1) + 1;
          const lineSlice = val.slice(lineStart, end);
          const unindented = lineSlice.replace(/^ {1,4}/gm, "");
          const diff = lineSlice.length - unindented.length;
          ta.setRangeText(unindented, lineStart, end, "end");
          ta.setSelectionRange(Math.max(lineStart, start - Math.min(4, diff)), end - diff);
        } else if (start !== end && val.slice(start, end).includes("\n")) {
          const lineStart = val.lastIndexOf("\n", start - 1) + 1;
          const lineSlice = val.slice(lineStart, end);
          const indented = lineSlice.replace(/^/gm, "    ");
          ta.setRangeText(indented, lineStart, end, "end");
        } else {
          ta.setRangeText("    ", start, end, "end");
        }
        ta.dispatchEvent(new Event("input", { bubbles: true }));
        return;
      }

      // Enter -> Smart auto-indent (preserve current indent + 4 spaces after ':')
      if (e.key === "Enter" && !e.ctrlKey && !e.metaKey && !e.shiftKey) {
        const lineStart = val.lastIndexOf("\n", start - 1) + 1;
        const currentLine = val.slice(lineStart, start);
        const indentMatch = currentLine.match(/^[ \t]*/);
        let indent = indentMatch ? indentMatch[0] : "";
        if (/:[ \t]*(#.*)?$/.test(currentLine)) {
          indent += "    ";
        }
        e.preventDefault();
        ta.setRangeText("\n" + indent, start, end, "end");
        ta.dispatchEvent(new Event("input", { bubbles: true }));
        return;
      }

      // Auto-close brackets & quotes when nothing is selected
      const pairs = { "(": ")", "[": "]", "{": "}" };
      if (pairs[e.key] && start === end) {
        const nextChar = val[start] || "";
        if (!/[A-Za-z0-9_]/.test(nextChar)) {
          e.preventDefault();
          ta.setRangeText(e.key + pairs[e.key], start, end, "start");
          ta.setSelectionRange(start + 1, start + 1);
          ta.dispatchEvent(new Event("input", { bubbles: true }));
        }
      }
    }

    toggleComment() {
      const ta = this.textarea;
      const val = ta.value;
      const start = ta.selectionStart;
      const end = ta.selectionEnd;
      const lineStart = val.lastIndexOf("\n", start - 1) + 1;
      const lineEndIdx = val.indexOf("\n", end);
      const blockEnd = lineEndIdx === -1 ? val.length : lineEndIdx;
      const block = val.slice(lineStart, blockEnd);
      const lines = block.split("\n");
      const allCommented = lines.filter(l => l.trim().length > 0).every(l => /^\s*#/.test(l));
      const updated = lines.map(l => {
        if (!l.trim()) return l;
        return allCommented ? l.replace(/^(\s*)#\s?/, "$1") : "# " + l;
      }).join("\n");
      ta.setRangeText(updated, lineStart, blockEnd, "select");
      ta.dispatchEvent(new Event("input", { bubbles: true }));
    }

    toggleFindBar() {
      const visible = this.findBar.style.display !== "none";
      this.findBar.style.display = visible ? "none" : "flex";
      if (!visible) {
        const inp = this.findBar.querySelector("#sl-cm6-find-input");
        if (inp) inp.focus();
      }
    }

    showAutocomplete() {
      this.acPopup.replaceChildren();
      for (const item of AICF_COMPLETIONS) {
        const row = document.createElement("div");
        row.className = "cm6-ac-item";
        row.innerHTML = `<span class="mono" style="font-weight:600;color:#38bdf8">${escapeHtml(item.label)}</span><span class="mut" style="font-size:11px">${escapeHtml(item.detail)}</span>`;
        row.addEventListener("click", () => {
          const ta = this.textarea;
          ta.setRangeText(item.insert, ta.selectionStart, ta.selectionEnd, "end");
          this.hideAutocomplete();
          ta.focus();
          ta.dispatchEvent(new Event("input", { bubbles: true }));
        });
        this.acPopup.appendChild(row);
      }
      this.acPopup.style.display = "block";
    }

    hideAutocomplete() {
      this.acPopup.style.display = "none";
    }

    _updateActiveLine() {
      const pos = this.textarea.selectionStart || 0;
      this.activeLine = this.textarea.value.slice(0, pos).split("\n").length;
    }

    setDiagnostics(diags) {
      this.diagnostics = Array.isArray(diags) ? diags : [];
      this._renderGutter();
    }

    goToLine(lineNo, fileName) {
      if (fileName && fileName !== this.activeFile && this.files[fileName] !== undefined) {
        this.switchFile(fileName);
      }
      const lines = this.textarea.value.split("\n");
      const target = Math.max(1, Math.min(lines.length, parseInt(lineNo, 10) || 1));
      let offset = 0;
      for (let i = 0; i < target - 1; i++) {
        offset += lines[i].length + 1;
      }
      this.textarea.focus();
      this.textarea.setSelectionRange(offset, offset + (lines[target - 1] || "").length);
      this.activeLine = target;
      this._renderGutter();
    }

    switchFile(filename) {
      if (!filename) return;
      this.files[this.activeFile] = this.textarea.value;
      if (this.files[filename] === undefined) {
        this.files[filename] = `# Tệp mô-đun phụ: ${filename}\n`;
      }
      this.activeFile = filename;
      this._suppressSync = true;
      this.textarea.value = this.files[filename];
      this._suppressSync = false;
      this._updateActiveLine();
      this.render();
    }

    deleteFile(filename, mainFile = "user_app.py") {
      if (filename === mainFile || !this.files[filename]) return false;
      delete this.files[filename];
      if (this.activeFile === filename) {
        this.switchFile(mainFile);
      }
      return true;
    }

    getAllFiles() {
      this.files[this.activeFile] = this.textarea.value;
      return { ...this.files };
    }

    setAllFiles(filesMap, mainFile = "user_app.py") {
      this.files = { ...filesMap };
      if (!this.files[mainFile]) {
        this.files[mainFile] = "";
      }
      this.activeFile = mainFile;
      this._suppressSync = true;
      this.textarea.value = this.files[mainFile];
      this._suppressSync = false;
      this.render();
    }

    getValue() {
      return this.textarea.value;
    }

    setValue(text) {
      this._suppressSync = true;
      this.textarea.value = String(text || "");
      this._suppressSync = false;
      this.files[this.activeFile] = this.textarea.value;
      this._updateActiveLine();
      this.render();
    }

    _renderGutter() {
      const lines = (this.textarea.value || "").split("\n");
      const diagByLine = new Map();
      for (const d of this.diagnostics) {
        if (d.file && d.file !== this.activeFile) continue;
        const ln = parseInt(d.line, 10) || 1;
        const prev = diagByLine.get(ln);
        if (!prev || d.severity === "error") {
          diagByLine.set(ln, d);
        }
      }
      const frag = document.createDocumentFragment();
      for (let i = 1; i <= lines.length; i++) {
        const row = document.createElement("div");
        const d = diagByLine.get(i);
        row.className = "cm6-gutter-line" +
          (i === this.activeLine ? " active" : "") +
          (d ? (d.severity === "error" ? " diag-err" : " diag-warn") : "");
        if (d) {
          row.title = `[${d.code}] ${d.message}`;
          row.textContent = (d.severity === "error" ? "● " : "▲ ") + i;
        } else {
          row.textContent = String(i);
        }
        frag.appendChild(row);
      }
      this.gutter.replaceChildren(frag);
    }

    render() {
      const raw = this.textarea.value || "";
      const lines = raw.split("\n");
      if (!this._lineCache) this._lineCache = new Map();
      if (this._lineCache.size > 10000) this._lineCache.clear();
      const htmlLines = new Array(lines.length);
      for (let i = 0; i < lines.length; i++) {
        const ln = lines[i];
        let h = this._lineCache.get(ln);
        if (h === undefined) {
          h = highlightPythonLine(ln);
          this._lineCache.set(ln, h);
        }
        htmlLines[i] = h;
      }
      this.highlightPre.innerHTML = htmlLines.join("\n") + "\n";
      this._renderGutter();
    }
  }

  global.AicfEditor = AicfEditor;
  global.computeUnifiedDiff = computeUnifiedDiff;
})(window);
