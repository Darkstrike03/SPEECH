/* Point this at your deployed backend.
   Local development falls back to the FastAPI server on port 8000. */
window.APP_CONFIG = {
  API_BASE: ["localhost", "127.0.0.1", ""].includes(location.hostname)
    ? "http://127.0.0.1:8000"
    : "https://speech-to-text-api-9ovp.onrender.com",
  MAX_UPLOAD_MB: 25,
  MAX_AUDIO_SECONDS: 300
};
