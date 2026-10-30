(() => {
  "use strict";

  const CONFIG = window.APP_CONFIG || {};
  const API_BASE = String(CONFIG.API_BASE || "").replace(/\/+$/, "");
  const WS_URL = `${API_BASE.replace(/^http/, "ws")}/ws/stream`;
  const MAX_UPLOAD_BYTES = (CONFIG.MAX_UPLOAD_MB || 25) * 1024 * 1024;
  const MAX_AUDIO_SECONDS = CONFIG.MAX_AUDIO_SECONDS || 300;
  const REQUEST_TIMEOUT_MS = 300_000;
  const SLOW_NOTICE_MS = 6_000;
  const LIVE_MAX_MS = 300_000;
  const PING_MS = 20_000;

  const $ = (id) => document.getElementById(id);
  const el = {
    tabUpload: $("tabUpload"),
    tabLive: $("tabLive"),
    panelUpload: $("panelUpload"),
    panelLive: $("panelLive"),
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
    micButton: $("micButton"),
    micHint: $("micHint"),
    liveStatus: $("liveStatus"),
    liveTimer: $("liveTimer"),
    liveDot: $("liveDot"),
    levelMeter: $("levelMeter"),
    liveLanguage: $("liveLanguage"),
    liveEngineHint: $("liveEngineHint"),
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

  const LEVEL_BARS = Array.from(el.levelMeter.querySelectorAll("i"));
  const WEIGHTS = [0.42, 0.6, 0.78, 0.92, 1, 1, 0.9, 0.74, 0.56, 0.84, 0.66, 0.46];

  const ENGINE_NOTES = {
    google: "SpeechRecognition · Google Web Speech. Fast, needs internet.",
    whisper: "On-server Whisper tiny. On free hosting expect ~3 min per audio minute."
  };

  const LIVE_ENGINE_NOTES = {
    google: "SpeechRecognition · Google Web Speech. One request per phrase.",
    whisper: "On-server Whisper tiny. Much slower on free hosting."
  };

  const LANGUAGE_NAMES = {
    auto: "Auto-detected", en: "English", hi: "Hindi", bn: "Bengali", ta: "Tamil",
    te: "Telugu", mr: "Marathi", gu: "Gujarati", ur: "Urdu", es: "Spanish",
    fr: "French", de: "German", pt: "Portuguese", ru: "Russian", ar: "Arabic",
    zh: "Chinese", ja: "Japanese", ko: "Korean", "en-US": "English"
  };

  const state = {
    mode: "upload",
    file: null,
    busy: false,
    userPickedLanguage: false,
    userPickedLiveLanguage: false,
    uploadFilename: "",
    result: emptyResult(),
    live: {
      active: false,
      stopping: false,
      ws: null,
      audioCtx: null,
      stream: null,
      worklet: null,
      analyser: null,
      raf: 0,
      timerId: 0,
      pingId: 0,
      startedAt: 0,
      reconnects: 0,
      seen: new Set(),
      status: "idle",
      language: "auto",
      engine: "google",
      ready: false,
      everReady: false,
      queued: false,
      connectSlowId: 0,
      connectLimitId: 0,
      doneResolver: null
    }
  };

  function emptyResult() {
    return { text: "", segments: [], engine: "", language: "", duration: 0, elapsed: 0, provider: "", filename: "" };
  }

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
    return `${String(mins).padStart(2, "0")}:${(seconds % 60).toFixed(1).padStart(4, "0")}`;
  }

  function setStatus(kind, text) {
    el.apiStatus.className = `status status--${kind}`;
    el.apiStatusText.textContent = text;
  }

  function showError(title, message) {
    el.errorTitle.textContent = title;
    el.errorMessage.textContent = message;
    el.errorBox.hidden = false;
  }

  function clearError() {
    el.errorBox.hidden = true;
  }

  function selectedEngine() {
    const checked = document.querySelector('input[name="engine"]:checked');
    return checked ? checked.value : "google";
  }

  function selectedLiveEngine() {
    const checked = document.querySelector('input[name="liveEngine"]:checked');
    return checked ? checked.value : "google";
  }

  function providerLabel(result) {
    if (result.engine === "google") return "Google Web Speech";
    if (result.engine === "whisper" && result.source === "live") return "Whisper · live";
    return "Whisper · on-server";
  }

  function syncEngineUI() {
    const engine = selectedEngine();
    el.engineHint.textContent = ENGINE_NOTES[engine];
    if (engine === "google" && el.language.value === "auto") {
      el.language.value = "en";
      el.languageHint.textContent = "Google needs a specific language; English selected.";
    } else if (engine === "whisper" && !state.userPickedLanguage) {
      el.language.value = "auto";
      el.languageHint.textContent = "Leave on detect unless the language is known.";
    }
  }

  function syncLiveEngineUI() {
    const engine = selectedLiveEngine();
    el.liveEngineHint.textContent = LIVE_ENGINE_NOTES[engine];
    if (engine === "google" && el.liveLanguage.value === "auto") {
      el.liveLanguage.value = "en";
    } else if (engine === "whisper" && !state.userPickedLiveLanguage) {
      el.liveLanguage.value = "auto";
    }
  }

  function setUploadBusy(busy) {
    state.busy = busy;
    el.transcribeBtn.disabled = busy || !state.file;
    el.transcribeBtn.classList.toggle("is-busy", busy);
    el.dropzone.classList.toggle("is-disabled", busy);
    el.clearFile.disabled = busy;
    el.progress.hidden = !busy;
    el.resetBtn.hidden = busy || !state.result.text;
    if (busy) el.progressText.textContent = "Sending the recording…";
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
    el.fileName.textContent = file.name;
    el.fileDetails.textContent = `${formatBytes(file.size)}${duration ? ` · ${formatClock(duration)}` : ""}`;
    el.fileCard.hidden = false;
    el.dropzone.hidden = true;
    el.transcribeBtn.disabled = false;
  }

  function clearFile() {
    state.file = null;
    el.fileInput.value = "";
    el.fileCard.hidden = true;
    el.dropzone.hidden = false;
    el.transcribeBtn.disabled = true;
  }

  function resetResult() {
    state.result = emptyResult();
    el.transcript.value = "";
    el.transcript.style.height = "";
    el.segmentsBody.replaceChildren();
    el.segmentsBlock.hidden = true;
    el.warnings.hidden = true;
    el.resultPanel.hidden = true;
    el.resetBtn.hidden = true;
    el.metaEngine.textContent = "—";
    el.metaLanguage.textContent = "—";
    el.metaDuration.textContent = "—";
    el.metaElapsed.textContent = "—";
  }

  function resetUpload() {
    clearFile();
    resetResult();
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

  function appendSegment(segment) {
    state.result.segments.push(segment);
    const row = document.createElement("tr");
    [formatStamp(segment.start), formatStamp(segment.end), segment.text].forEach((value, index) => {
      const cell = document.createElement("td");
      cell.textContent = value;
      if (index < 2) cell.className = "mono";
      row.appendChild(cell);
    });
    el.segmentsBody.appendChild(row);
    el.segmentsBlock.hidden = false;
  }

  function renderMeta() {
    const result = state.result;
    if (!result.engine) return;
    el.metaEngine.textContent = providerLabel(result);
    el.metaLanguage.textContent = LANGUAGE_NAMES[result.language] || result.language || "—";
    el.metaDuration.textContent = result.duration ? formatClock(result.duration) : "—";
    el.metaElapsed.textContent = result.elapsed ? `${result.elapsed.toFixed(1)} s` : "—";
  }

  function showResultPanel() {
    el.resultPanel.hidden = false;
  }

  function autoSizeTranscript() {
    el.transcript.style.height = "auto";
    const max = Math.max(220, Math.round(window.innerHeight * 0.55));
    const height = Math.min(Math.max(el.transcript.scrollHeight, 190), max);
    el.transcript.style.height = `${height}px`;
  }

  async function readError(response) {
    try {
      const data = await response.json();
      if (data && data.error) return data.error;
    } catch (_) { /* ignore */ }
    return { message: `Request failed with status ${response.status}.` };
  }

  async function transcribeFile() {
    if (!state.file || state.busy) return;
    clearError();
    resetResult();
    setUploadBusy(true);

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    const slow = setTimeout(() => {
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

      const data = await response.json();
      setStatus("ok", "Server online");
      state.result = {
        text: data.text || "",
        segments: data.segments || [],
        engine: data.engine,
        language: data.language,
        duration: data.duration,
        elapsed: data.elapsed,
        provider: data.provider,
        source: "upload",
        filename: data.filename
      };
      el.transcript.value = state.result.text;
      autoSizeTranscript();
      el.segmentsBody.replaceChildren();
      (data.segments || []).forEach((segment) => {
        const row = document.createElement("tr");
        [formatStamp(segment.start), formatStamp(segment.end), segment.text].forEach((value, index) => {
          const cell = document.createElement("td");
          cell.textContent = value;
          if (index < 2) cell.className = "mono";
          row.appendChild(cell);
        });
        el.segmentsBody.appendChild(row);
      });
      el.segmentsBlock.hidden = !(data.segments || []).length;
      renderWarnings(data.warnings);
      renderMeta();
      showResultPanel();
      el.resetBtn.hidden = false;
      el.resultPanel.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) {
      if (error.name === "AbortError") {
        showError("Request timed out", "The server took too long. Try a shorter clip.");
      } else {
        showError("Cannot reach the server", "Check the connection, or try again once the server finishes waking up.");
      }
      setStatus("error", "Server unreachable");
    } finally {
      clearTimeout(timer);
      clearTimeout(slow);
      setUploadBusy(false);
    }
  }

  function activateTab(mode) {
    if (mode === state.mode) return;
    if (state.live.active) stopLive();
    state.mode = mode;
    const upload = mode === "upload";

    el.tabUpload.classList.toggle("is-active", upload);
    el.tabUpload.setAttribute("aria-selected", String(upload));
    el.tabUpload.tabIndex = upload ? 0 : -1;
    el.tabLive.classList.toggle("is-active", !upload);
    el.tabLive.setAttribute("aria-selected", String(!upload));
    el.tabLive.tabIndex = upload ? -1 : 0;

    el.panelUpload.hidden = !upload;
    el.panelLive.hidden = upload;

    clearError();
  }

  function setLiveUI(active, statusText) {
    el.micButton.classList.toggle("is-recording", active);
    el.micButton.setAttribute("aria-pressed", String(active));
    el.micButton.setAttribute("aria-label", active ? "Stop listening" : "Start listening");
    el.micHint.textContent = active ? "Tap to stop" : "Tap to speak";
    el.levelMeter.classList.toggle("is-active", active);
    el.liveDot.classList.toggle("is-live", active);
    document.querySelectorAll('input[name="liveEngine"]').forEach((input) => {
      input.disabled = active;
    });
    el.liveLanguage.disabled = active;
    if (statusText) el.liveStatus.textContent = statusText;
  }

  function startTimer() {
    state.live.startedAt = Date.now();
    const tick = () => {
      const elapsed = (Date.now() - state.live.startedAt) / 1000;
      el.liveTimer.textContent = formatClock(elapsed);
      if (elapsed * 1000 >= LIVE_MAX_MS) stopLive();
    };
    tick();
    state.live.timerId = window.setInterval(tick, 500);
  }

  function stopTimer() {
    if (state.live.timerId) window.clearInterval(state.live.timerId);
    state.live.timerId = 0;
    el.liveTimer.textContent = "00:00";
  }

  function startMeter() {
    const analyser = state.live.analyser;
    if (!analyser) return;
    const data = new Uint8Array(analyser.fftSize);
    const loop = () => {
      if (state.live.analyser !== analyser) return;
      analyser.getByteTimeDomainData(data);
      let sum = 0;
      for (let i = 0; i < data.length; i += 1) {
        const value = (data[i] - 128) / 128;
        sum += value * value;
      }
      const rms = Math.sqrt(sum / data.length);
      const level = Math.min(1, rms * 6.5);
      el.levelMeter.classList.toggle("is-live", level > 0.06);
      LEVEL_BARS.forEach((bar, index) => {
        const height = Math.max(0.06, Math.min(1, level * WEIGHTS[index] * 1.5));
        bar.style.transform = `scaleY(${height})`;
      });
      state.live.raf = requestAnimationFrame(loop);
    };
    state.live.raf = requestAnimationFrame(loop);
  }

  function stopMeter() {
    if (state.live.raf) cancelAnimationFrame(state.live.raf);
    state.live.raf = 0;
    el.levelMeter.classList.remove("is-live");
    LEVEL_BARS.forEach((bar) => { bar.style.transform = "scaleY(0.08)"; });
  }

  function sendJson(payload) {
    const ws = state.live.ws;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(payload));
      return true;
    }
    return false;
  }

  function clearConnectWatch() {
    if (state.live.connectSlowId) window.clearTimeout(state.live.connectSlowId);
    if (state.live.connectLimitId) window.clearTimeout(state.live.connectLimitId);
    state.live.connectSlowId = 0;
    state.live.connectLimitId = 0;
  }

  function startConnectWatch() {
    clearConnectWatch();
    state.live.connectSlowId = window.setTimeout(() => {
      if (!state.live.active || state.live.ready || state.live.queued) return;
      setLiveUI(true, "Waking the server… free hosting can take up to a minute.");
    }, 6000);
    state.live.connectLimitId = window.setTimeout(() => {
      if (!state.live.active || state.live.ready || state.live.queued) return;
      showError(
        "Server is not responding",
        "The live API did not answer within 60 seconds. Wait a moment and tap the mic to try again."
      );
      stopLive();
    }, 60000);
  }

  function handleServerMessage(payload) {
    switch (payload.type) {
      case "queued": {
        state.live.ready = false;
        state.live.queued = true;
        clearConnectWatch();
        const position = Number(payload.position) || 1;
        setLiveUI(true, `Waiting for a live slot (#${position})…`);
        break;
      }
      case "ready":
        state.live.ready = true;
        state.live.everReady = true;
        state.live.queued = false;
        clearConnectWatch();
        state.live.status = "listening";
        if (!state.live.timerId) startTimer();
        setLiveUI(true, "Listening");
        break;
      case "vad":
        if (payload.speaking) {
          setLiveUI(true, "Listening");
        } else {
          setLiveUI(true, "Transcribing…");
        }
        break;
      case "processing":
        setLiveUI(true, "Transcribing…");
        break;
      case "final":
        if (state.live.seen.has(payload.seq)) return;
        state.live.seen.add(payload.seq);
        appendFinal(payload);
        setLiveUI(true, "Listening");
        break;
      case "warning":
        appendLiveWarning(payload.message);
        break;
      case "error":
        if (payload.code === "too_long") {
          showError("Session limit reached", payload.message);
          stopLive();
        } else if (payload.code === "stream_full") {
          showError("Live queue is full", payload.message);
          stopLive();
        } else if (payload.code === "live_queue_timeout") {
          showError("No live slot opened up", payload.message);
          stopLive();
        } else if (payload.code === "idle_timeout") {
          showError("Live session ended", payload.message);
          stopLive();
        } else {
          appendLiveWarning(payload.message);
        }
        break;
      case "done":
        setLiveUI(true, "Finishing…");
        if (state.live.doneResolver) {
          const resolve = state.live.doneResolver;
          state.live.doneResolver = null;
          resolve();
        }
        break;
      default:
        break;
    }
  }

  function appendLiveWarning(message) {
    const existing = Array.from(el.warnings.querySelectorAll("li")).map((item) => item.textContent);
    if (existing.includes(message)) return;
    renderWarnings([...existing, message]);
  }

  function appendFinal(payload) {
    const text = (payload.text || "").trim();
    if (!text) return;
    if (state.result.language !== payload.language) {
      state.result.language = payload.language;
      renderMeta();
    }
    state.result.text = state.result.text ? `${state.result.text} ${text}` : text;
    state.result.engine = payload.engine || "google";
    state.result.provider = payload.provider || "google";
    state.result.source = "live";
    state.result.duration = payload.end;
    state.result.elapsed = payload.elapsed || state.result.elapsed;
    state.result.filename = "live-transcript";

    el.transcript.value = state.result.text;
    autoSizeTranscript();
    appendSegment({ start: payload.start, end: payload.end, text });
    renderMeta();
    showResultPanel();
    (payload.warnings || []).forEach(appendLiveWarning);
    el.transcript.scrollTop = el.transcript.scrollHeight;
  }

  async function ensureWorklet(ctx) {
    if (!ctx.audioWorklet) throw new Error("AudioWorklet is not supported in this browser.");
    await ctx.audioWorklet.addModule("pcm-worklet.js");
  }

  function openSocket() {
    startConnectWatch();
    const ws = new WebSocket(WS_URL);
    state.live.ws = ws;

    ws.onopen = () => {
      state.live.reconnects = 0;
      ws.send(JSON.stringify({
        type: "start",
        language: state.live.language,
        engine: state.live.engine
      }));
      if (state.live.pingId) window.clearInterval(state.live.pingId);
      state.live.pingId = window.setInterval(() => sendJson({ type: "ping" }), PING_MS);
    };

    ws.onmessage = (event) => {
      let payload;
      try {
        payload = JSON.parse(event.data);
      } catch (_) {
        return;
      }
      handleServerMessage(payload);
    };

    ws.onerror = () => {
      if (state.live.active) setLiveUI(true, "Connection issue");
    };

    ws.onclose = () => {
      if (state.live.pingId) window.clearInterval(state.live.pingId);
      state.live.pingId = 0;
      state.live.ready = false;
      state.live.queued = false;
      if (!state.live.active || state.live.stopping) return;

      if (!state.live.everReady) {
        showError(
          "Cannot reach the live API",
          "The microphone stream never connected. An ad blocker or privacy extension can block the API; try Incognito or allow speech-to-text-api-9ovp.onrender.com."
        );
        stopLive();
        return;
      }

      if (state.live.reconnects < 2) {
        state.live.reconnects += 1;
        setLiveUI(true, "Reconnecting…");
        window.setTimeout(() => {
          if (state.live.active) openSocket();
        }, 1200);
      } else {
        showError("Live connection lost", "The transcription server closed the stream. Tap the mic to try again.");
        stopLive();
      }
    };
  }

  async function startLive() {
    if (state.live.active) return;
    clearError();
    resetResult();

    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      showError("Microphone unavailable", "This browser does not expose microphone access on this page. Use HTTPS or localhost in a recent browser.");
      return;
    }

    state.live.language = el.liveLanguage.value;
    state.live.engine = selectedLiveEngine();

    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true }
      });
      state.live.stream = stream;

      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      await ctx.resume();
      await ensureWorklet(ctx);
      state.live.audioCtx = ctx;

      const source = ctx.createMediaStreamSource(stream);
      const filter = ctx.createBiquadFilter();
      filter.type = "lowpass";
      filter.frequency.value = 7000;
      filter.Q.value = 0.7;

      const worklet = new AudioWorkletNode(ctx, "pcm-worklet", {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        outputChannelCount: [1],
        processorOptions: { targetRate: 16000, chunkSamples: 1600 }
      });
      const silence = ctx.createGain();
      silence.gain.value = 0;

      source.connect(filter);
      filter.connect(worklet);
      worklet.connect(silence);
      silence.connect(ctx.destination);

      const analyser = ctx.createAnalyser();
      analyser.fftSize = 1024;
      analyser.smoothingTimeConstant = 0.7;
      source.connect(analyser);

      state.live.worklet = worklet;
      state.live.analyser = analyser;
      state.live.seen = new Set();
      state.live.reconnects = 0;
      state.live.ready = false;
      state.live.everReady = false;
      state.live.queued = false;
      state.live.stopping = false;
      state.live.active = true;

      worklet.port.onmessage = (event) => {
        const ws = state.live.ws;
        if (state.live.ready && ws && ws.readyState === WebSocket.OPEN) ws.send(event.data);
      };

      setLiveUI(true, "Connecting…");
      startMeter();
      openSocket();
    } catch (error) {
      cleanupAudio();
      state.live.active = false;
      setLiveUI(false, "Microphone idle");
      const message = error && error.name === "NotAllowedError"
        ? "Microphone access was blocked. Allow it in the browser's site settings and try again."
        : error && error.name === "NotFoundError"
          ? "No microphone was found on this device."
          : (error && error.message) || "The microphone could not be opened.";
      showError("Microphone unavailable", message);
    }
  }

  function cleanupAudio() {
    stopMeter();
    stopTimer();
    clearConnectWatch();
    state.live.ready = false;
    state.live.queued = false;
    if (state.live.pingId) window.clearInterval(state.live.pingId);
    state.live.pingId = 0;

    const { worklet, stream, audioCtx, ws } = state.live;
    state.live.worklet = null;
    state.live.analyser = null;
    state.live.stream = null;
    state.live.audioCtx = null;

    if (worklet) {
      worklet.port.onmessage = null;
      try { worklet.disconnect(); } catch (_) { /* ignore */ }
    }
    if (stream) stream.getTracks().forEach((track) => track.stop());
    if (audioCtx && audioCtx.state !== "closed") audioCtx.close().catch(() => {});
    if (ws) {
      try { ws.close(); } catch (_) { /* ignore */ }
    }
  }

  async function stopLive() {
    if (!state.live.active) return;
    state.live.stopping = true;
    state.live.active = false;

    const ws = state.live.ws;
    if (ws && ws.readyState === WebSocket.OPEN && state.live.ready) {
      const finished = new Promise((resolve) => {
        state.live.doneResolver = resolve;
        setTimeout(resolve, 8000);
      });
      try { ws.send(JSON.stringify({ type: "stop" })); } catch (_) { /* ignore */ }
      setLiveUI(true, "Finishing…");
      await finished;
    }

    state.live.doneResolver = null;
    cleanupAudio();
    state.live.ws = null;
    setLiveUI(false, "Microphone idle");
    if (state.result.text) el.resetBtn.hidden = false;
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
    if (!state.result.text) return;
    const base = (state.result.filename || "transcript").replace(/\.[^.]+$/, "");
    const header = `${providerLabel(state.result)} | ${state.result.language || "auto"} | ${formatClock(state.result.duration)}\n\n`;
    const blob = new Blob([header + state.result.text + "\n"], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${base}.txt`;
    link.click();
    URL.revokeObjectURL(url);
  }

  async function warmUp() {
    setStatus("idle", "Checking the scriptorium…");
    const slow = setTimeout(() => setStatus("idle", "Waking server…"), 4000);
    try {
      const response = await fetch(`${API_BASE}/health`, { cache: "no-store" });
      clearTimeout(slow);
      if (!response.ok) {
        setStatus("error", "Server error");
        return;
      }
      await response.json();
      setStatus("ok", "Server online");
    } catch (_) {
      clearTimeout(slow);
      setStatus("error", "Server offline");
    }
  }

  el.tabUpload.addEventListener("click", () => activateTab("upload"));
  el.tabLive.addEventListener("click", () => activateTab("live"));

  [el.tabUpload, el.tabLive].forEach((tab) => {
    tab.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      const next = state.mode === "upload" ? "live" : "upload";
      activateTab(next);
      (next === "upload" ? el.tabUpload : el.tabLive).focus();
    });
  });

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
    const file = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0];
    if (file) selectFile(file);
  });
  window.addEventListener("dragover", (event) => event.preventDefault());
  window.addEventListener("drop", (event) => event.preventDefault());

  el.clearFile.addEventListener("click", clearFile);
  el.transcribeBtn.addEventListener("click", transcribeFile);
  el.resetBtn.addEventListener("click", () => {
    if (state.mode === "upload") {
      resetUpload();
    } else {
      resetResult();
      clearError();
    }
  });
  el.copyBtn.addEventListener("click", copyTranscript);
  el.downloadBtn.addEventListener("click", downloadTranscript);
  el.micButton.addEventListener("click", () => {
    if (state.live.active) stopLive();
    else startLive();
  });

  document.querySelectorAll('input[name="engine"]').forEach((input) => {
    input.addEventListener("change", syncEngineUI);
  });
  el.language.addEventListener("change", () => { state.userPickedLanguage = true; });

  document.querySelectorAll('input[name="liveEngine"]').forEach((input) => {
    input.addEventListener("change", syncLiveEngineUI);
  });
  el.liveLanguage.addEventListener("change", () => { state.userPickedLiveLanguage = true; });
  window.addEventListener("beforeunload", () => {
    if (state.live.active) {
      state.live.stopping = true;
      try { state.live.ws && state.live.ws.close(); } catch (_) { /* ignore */ }
      cleanupAudio();
    }
  });

  el.apiDocsLink.href = `${API_BASE}/docs`;
  syncEngineUI();
  syncLiveEngineUI();
  warmUp();
})();
