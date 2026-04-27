(function () {
  const shareId = location.pathname.split("/").filter(Boolean).pop();

  function fmtBytes(n) {
    if (n >= 1e9) return (n / 1e9).toFixed(2) + " GB";
    if (n >= 1e6) return (n / 1e6).toFixed(1) + " MB";
    if (n >= 1e3) return (n / 1e3).toFixed(0) + " KB";
    return n + " B";
  }

  function fmtDate(iso) {
    if (!iso) return "Unknown date";
    const d = new Date(iso);
    return d.toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" });
  }

  function fileIcon(name) {
    const ext = (name.split(".").pop() || "").toLowerCase();
    const map = {
      pdf: "📕", zip: "🗜️", gz: "🗜️", tar: "🗜️", "7z": "🗜️", rar: "🗜️",
      mp4: "🎬", mkv: "🎬", avi: "🎬", mov: "🎬", webm: "🎬",
      mp3: "🎵", wav: "🎵", flac: "🎵", aac: "🎵",
      jpg: "🖼️", jpeg: "🖼️", png: "🖼️", gif: "🖼️", svg: "🖼️", webp: "🖼️",
      txt: "📝", md: "📝", csv: "📊", xlsx: "📊", xls: "📊",
      docx: "📄", doc: "📄", pptx: "📊", ppt: "📊",
      exe: "⚙️", dmg: "⚙️", apk: "📱",
      js: "💻", ts: "💻", py: "💻", html: "💻", css: "💻", json: "💻",
    };
    return map[ext] || "📄";
  }

  async function init() {
    try {
      const res = await fetch(`/api/f/${shareId}`);
      if (!res.ok) throw new Error("not found");
      const data = await res.json();

      document.title = `DataDock – ${data.filename}`;
      document.getElementById("lc-icon").textContent = fileIcon(data.filename);
      document.getElementById("lc-filename").textContent = data.filename;
      document.getElementById("lc-size").textContent = fmtBytes(data.file_size);
      document.getElementById("lc-date").textContent = fmtDate(data.completed_at);
      document.getElementById("lc-views").textContent = (data.views || 0).toLocaleString();
      document.getElementById("lc-downloads").textContent = (data.downloads || 0).toLocaleString();
      document.getElementById("lc-download-btn").href = `/api/f/${shareId}/download`;

      document.getElementById("lc-loading").hidden = true;
      document.getElementById("lc-card").hidden = false;
    } catch {
      document.getElementById("lc-loading").hidden = true;
      document.getElementById("lc-error").hidden = false;
    }
  }

  init();
})();
