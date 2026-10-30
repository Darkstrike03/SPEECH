(() => {
  "use strict";

  const CONFIG = window.APP_CONFIG || {};
  const API_BASE = String(CONFIG.API_BASE || "").replace(/\/+$/, "");
  const MAX_UPLOAD_BYTES = (CONFIG.MAX_UPLOAD_MB || 25) * 1024 * 1024;
  const MAX_AUDIO_SECONDS = CONFIG.MAX_AUDIO_SECONDS || 300;
  const REQUEST_TIMEOUT_MS = 300_000;
  const SLOW_NOTICE_MS = 6_000;

  const $ = (id) => document.getElementById(id);
  const el = {
    dropzone: $("dropzone"),
    fileInput: $("fileInput"),
    fileCard: $("fileCard"),
    fileName: $("fileName"),
    fileDetails: $("fileDetails"),
    clearFile: $("clearFile"),
    language: $("language"),
    languageHint: $("languageHint"),
    engineHint: $("engineHint"),
    transcribeBtn: $("transcribeBtn"),
    resetBtn: $("resetBtn"),
    progress: $("progress"),
    progressText: $("progressText"),
    errorBox: $("errorBox"),
    errorTitle: $("errorTitle"),
    errorMessage: $("errorMessage"),
    resultPanel: $("resultPanel"),
    transcript: $("transcript"),
    metaEngine: $("metaEngine"),
    metaLanguage: $("metaLanguage"),
    metaDuration: $("metaDuration"),
    metaElapsed: $("metaElapsed"),
    warnings: $("warnings"),
    segmentsBlock: $("segmentsBlock"),
    segmentsBody: $("segmentsBody"),
    copyBtn: $("copyBtn"),
    downloadBtn: $("downloadBtn"),
    apiStatus: $("apiStatus"),
    apiStatusText: $("apiStatusText"),
    apiDocsLink: $("apiDocsLink")
  };

  const state = {
    file: null,
    audioDuration: null,
    busy: false,
    userPickedLanguage: false,
    result: null
  };

  const ENGINE_NOTES = {
    whisper: "Runs locally on the server. Timestamps included.",
    google: "Fast, but Google's free endpoint can be rate-limited."
  };

  const LANGUAGE_NAMES = {
    auto: "Auto-detected", en: "English", hi: "Hindi", bn: "Bengali", ta: "Tamil",
    te: "Telugu", mr: "Marathi", gu: "Gujarati", ur: "Urdu", es: "Spanish",
    fr: "French", de: "German", pt: "Portuguese", ru: "Russian", ar: "Arabic",
    zh: "Chinese", ja: "Japanese", ko: "Korean", "en-US": "English"
  };

  function formatBytes(bytes) {
    if (!bytes) return "0 B";
    const units = ["B", "KB", "MB", "GB"];
    const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
    return `${(bytes / Math.pow(1024, index)).toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
  }

  function formatClock(seconds) {
    if (!Number.isFinite(seconds)) return "—";
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    return `${mins}:${String(secs).padStart(2, "0")}`;
  }

  function formatStamp(seconds) {
    if (!Number.isFinite(seconds)) return "—";
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${String(mins).padStart(2, "0")}:${secs.toFixed(1).padStart(4, "0")}`;
  }

  function setStatus(kind, text) {
    el.apiStatus.className = `status status--${kind}`;
    el.apiStatusText.textContent = text;
  }

  function selectedEngine() {
    const checked = document.querySelector('input[name="engine"]:checked');
    return checked ? checked.value : "whisper";
  }

  function syncEngineUI() {
    const engine = selectedEngine();
    el.engineHint.textContent = ENGINE_NOTES[engine];
    el.language.disabled = false;
    el.languageHint.textContent = engine === "google"
      ? "Google needs a specific language; auto falls back to English."
      : "Leave on auto unless the language is known.";
    if (engine === "google" && el.language.value === "auto") {
      el.language.value = "en";
      el.languageHint.textContent = "Google Web Speech has no auto-detect, so English was selected.";
    } else if (engine === "whisper" && !state.userPickedLanguage) {
      el.language.value = "auto";
    }
  }

  function showError(title, message) {
    el.errorTitle.textContent = title;
    el.errorMessage.textContent = message;
    el.errorBox.hidden = false;
  }

  function clearError() {
    el.errorBox.hidden = true;
  }

  function setBusy(busy) {
    state.busy = busy;
    el.transcribeBtn.disabled = busy || !state.file;
    el.transcribeBtn.classList.toggle("is-busy", busy);
    el.dropzone.classList.toggle("is-disabled", busy);
    el.clearFile.disabled = busy;
    el.progress.hidden = !busy;
    el.resetBtn.hidden = busy || !state.result;
    if (busy) {
      el.progressText.textContent = "Uploading audio…";
      el.resetBtn.hidden = true;
    }
  }

  function probeDuration(file) {
    return new Promise((resolve) => {
      const url = URL.createObjectURL(file);
      const audio = new Audio();
      const done = (value) => {
        URL.revokeObjectURL(url);
        resolve(value);
      };
      audio.preload = "metadata";
      audio.onloadedmetadata = () => done(Number.isFinite(audio.duration) ? audio.duration : null);
      audio.onerror = () => done(null);
      setTimeout(() => done(null), 4000);
      audio.src = url;
    });
  }

  async function selectFile(file) {
    if (!file) return;
    clearError();

    if (file.size > MAX_UPLOAD_BYTES) {
      showError("File too large", `"${file.name}" is ${formatBytes(file.size)}. The limit is ${formatBytes(MAX_UPLOAD_BYTES)}.`);
      return;
    }

    const duration = await probeDuration(file);
    if (duration && duration > MAX_AUDIO_SECONDS + 1) {
      showError("Audio too long", `This recording is ${formatClock(duration)} long. The limit is ${formatClock(MAX_AUDIO_SECONDS)}.`);
      return;
    }

    state.file = file;
    state.audioDuration = duration;
    el.fileName.textContent = file.name;
    el.fileDetails.textContent = `${formatBytes(file.size)}${duration ? ` · ${formatClock(duration)}` : ""}`;
    el.fileCard.hidden = false;
    el.dropzone.hidden = true;
    el.transcribeBtn.disabled = false;
  }

  function clearFile() {
    state.file = null;
    state.audioDuration = null;
    el.fileInput.value = "";
    el.fileCard.hidden = true;
    el.dropzone.hidden = false;
    el.transcribeBtn.disabled = true;
  }

  function resetAll() {
    clearFile();
    state.result = null;
    state.userPickedLanguage = false;
    el.resultPanel.hidden = true;
    el.resetBtn.hidden = true;
    el.segmentsBlock.hidden = true;
    el.warnings.hidden = true;
    el.transcript.value = "";
    el.segmentsBody.replaceChildren();
    clearError();
    el.language.value = selectedEngine() === "google" ? "en" : "auto";
    el.dropzone.focus();
  }

  function renderWarnings(warnings) {
    el.warnings.replaceChildren();
    if (!warnings || !warnings.length) {
      el.warnings.hidden = true;
      return;
    }
    const list = document.createElement("ul");
    warnings.forEach((warning) => {
      const item = document.createElement("li");
      item.textContent = warning;
      list.appendChild(item);
    });
    el.warnings.appendChild(list);
    el.warnings.hidden = false;
  }

  function renderSegments(segments) {
    el.segmentsBody.replaceChildren();
    if (!segments || !segments.length) {
      el.segmentsBlock.hidden = true;
      return;
    }
    segments.forEach((segment) => {
      const row = document.createElement("tr");
      [formatStamp(segment.start), formatStamp(segment.end), segment.text].forEach((value, index) => {
        const cell = document.createElement(index === 2 ? "td" : "td");
        cell.textContent = value;
        if (index < 2) cell.className = "mono";
        row.appendChild(cell);
      });
      el.segmentsBody.appendChild(row);
    });
    el.segmentsBlock.hidden = false;
  }

  function renderResult(result, filename) {
    state.result = { ...result, filename };
    el.transcript.value = result.text || "";
    el.metaEngine.textContent = result.engine === "whisper" ? "Whisper tiny" : "Google Web Speech";
    el.metaLanguage.textContent = LANGUAGE_NAMES[result.language] || result.language || "—";
    el.metaDuration.textContent = formatClock(result.duration);
    el.metaElapsed.textContent = `${result.elapsed.toFixed(1)} s`;
    renderWarnings(result.warnings);
    renderSegments(result.segments);
    el.resultPanel.hidden = false;
    el.resetBtn.hidden = false;
    el.resultPanel.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  async function readError(response) {
    try {
      const data = await response.json();
      if (data && data.error) return data.error;
    } catch (_) { /* ignore */ }
    return { message: `Request failed with status ${response.status}.` };
  }

  async function transcribe() {
    if (!state.file || state.busy) return;
    clearError();
    el.resultPanel.hidden = true;
    setBusy(true);

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    const slowTimer = setTimeout(() => {
      el.progressText.textContent = "Still working — the free server may be waking up (up to a minute)…";
    }, SLOW_NOTICE_MS);

    const form = new FormData();
    form.append("file", state.file, state.file.name);
    form.append("engine", selectedEngine());
    form.append("language", el.language.value);

    try {
      const response = await fetch(`${API_BASE}/api/transcribe`, {
        method: "POST",
        body: form,
        signal: controller.signal
      });

      if (!response.ok) {
        const error = await readError(response);
        showError("Transcription failed", error.message || "The server rejected this file.");
        setStatus("error", "Request failed");
        return;
      }

      const result = await response.json();
      setStatus("ok", "Server online");
      renderResult(result, state.file.name);
    } catch (error) {
      if (error.name === "AbortError") {
        showError("Request timed out", "The server took too long to respond. Try a shorter clip or the Whisper engine.");
      } else {
        showError("Cannot reach the server", "Check your connection, or try again once the server finishes starting up.");
      }
      setStatus("error", "Server unreachable");
    } finally {
      clearTimeout(timer);
      clearTimeout(slowTimer);
      setBusy(false);
    }
  }

  async function copyTranscript() {
    const text = el.transcript.value;
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
    } catch (_) {
      el.transcript.select();
      document.execCommand("copy");
      el.transcript.setSelectionRange(0, 0);
    }
    const original = el.copyBtn.textContent;
    el.copyBtn.textContent = "Copied";
    setTimeout(() => { el.copyBtn.textContent = original; }, 1500);
  }

  function downloadTranscript() {
    if (!state.result) return;
    const base = (state.result.filename || "transcript").replace(/\.[^.]+$/, "");
    const header = `Engine: ${state.result.engine} | Language: ${state.result.language} | Duration: ${formatClock(state.result.duration)}\n\n`;
    const blob = new Blob([header + (state.result.text || "") + "\n"], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${base}.txt`;
    link.click();
    URL.revokeObjectURL(url);
  }

  async function warmUp() {
    setStatus("idle", "Checking server…");
    const slow = setTimeout(() => setStatus("idle", "Waking server…"), 4000);
    try {
      const response = await fetch(`${API_BASE}/health`, { cache: "no-store" });
      clearTimeout(slow);
      if (response.ok) setStatus("ok", "Server online");
      else setStatus("error", "Server error");
    } catch (_) {
      clearTimeout(slow);
      setStatus("error", "Server offline");
    }
  }

  el.dropzone.addEventListener("click", () => el.fileInput.click());
  el.dropzone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      el.fileInput.click();
    }
  });
  el.fileInput.addEventListener("change", () => selectFile(el.fileInput.files[0]));

  let dragDepth = 0;
  el.dropzone.addEventListener("dragenter", (event) => {
    event.preventDefault();
    dragDepth += 1;
    el.dropzone.classList.add("is-dragging");
  });
  el.dropzone.addEventListener("dragover", (event) => event.preventDefault());
  el.dropzone.addEventListener("dragleave", () => {
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) el.dropzone.classList.remove("is-dragging");
  });
  el.dropzone.addEventListener("drop", (event) => {
    event.preventDefault();
    dragDepth = 0;
    el.dropzone.classList.remove("is-dragging");
    const file = event.dataTransfer?.files?.[0];
    if (file) selectFile(file);
  });
  window.addEventListener("dragover", (event) => event.preventDefault());
  window.addEventListener("drop", (event) => event.preventDefault());

  el.clearFile.addEventListener("click", clearFile);
  el.transcribeBtn.addEventListener("click", transcribe);
  el.resetBtn.addEventListener("click", resetAll);
  el.copyBtn.addEventListener("click", copyTranscript);
  el.downloadBtn.addEventListener("click", downloadTranscript);

  document.querySelectorAll('input[name="engine"]').forEach((input) => {
    input.addEventListener("change", syncEngineUI);
  });
  el.language.addEventListener("change", () => {
    state.userPickedLanguage = true;
  });

  el.apiDocsLink.href = `${API_BASE}/docs`;
  syncEngineUI();
  warmUp();
})();
