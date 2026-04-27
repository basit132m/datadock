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
    return new Date(iso).toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" });
  }

  function fileIcon(name) {
    const ext = (name.split(".").pop() || "").toLowerCase();
    const map = {
      pdf: "📕", zip: "🗜️", gz: "🗜️", tar: "🗜️", "7z": "🗜️", rar: "🗜️",
      mp4: "🎬", mkv: "🎬", avi: "🎬", mov: "🎬", webm: "🎬",
      mp3: "🎵", wav: "🎵", flac: "🎵", aac: "🎵",
      jpg: "🖼️", jpeg: "🖼️", png: "🖼️", gif: "🖼️", svg: "🖼️", webp: "🖼️",
      txt: "📝", md: "📝", csv: "📊", xlsx: "📊", xls: "📊",
      docx: "📄", doc: "📄", exe: "⚙️", dmg: "⚙️", apk: "📱",
      js: "💻", ts: "💻", py: "💻", html: "💻", css: "💻", json: "💻",
    };
    return map[ext] || "📄";
  }

  function renderAds(ads) {
    const banners = ads.filter(a => a.type === "banner");
    const buttons = ads.filter(a => a.type === "button");

    // Split banners: first half top, second half bottom
    const mid = Math.ceil(banners.length / 2);
    const topBanners    = banners.slice(0, mid);
    const bottomBanners = banners.slice(mid);

    const topSlot    = document.getElementById("ads-top");
    const bottomSlot = document.getElementById("ads-bottom");
    const btnSlot    = document.getElementById("ads-buttons");

    topBanners.forEach(ad => {
      topSlot.appendChild(makeBanner(ad));
    });

    bottomBanners.forEach(ad => {
      bottomSlot.appendChild(makeBanner(ad));
    });

    buttons.forEach(ad => {
      btnSlot.appendChild(makeButton(ad));
    });
  }

  function makeBanner(ad) {
    const a = document.createElement("a");
    a.href = ad.link_url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.className = "ad-banner";
    if (ad.image_url) {
      const img = document.createElement("img");
      img.src = ad.image_url;
      img.alt = ad.label;
      img.loading = "lazy";
      a.appendChild(img);
    } else {
      a.textContent = ad.label;
      a.classList.add("ad-banner-text");
    }
    return a;
  }

  function makeButton(ad) {
    const a = document.createElement("a");
    a.href = ad.link_url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.className = "ad-btn";
    a.textContent = ad.label;
    return a;
  }

  async function init() {
    // Fetch file info and ads in parallel
    const [fileResult, adsResult] = await Promise.allSettled([
      fetch(`/api/f/${shareId}`).then(r => r.ok ? r.json() : Promise.reject()),
      fetch("/api/ads").then(r => r.ok ? r.json() : []),
    ]);

    // Render ads regardless of file result
    if (adsResult.status === "fulfilled" && Array.isArray(adsResult.value)) {
      renderAds(adsResult.value);
    }

    if (fileResult.status !== "fulfilled") {
      document.getElementById("lc-loading").hidden = true;
      document.getElementById("lc-error").hidden = false;
      return;
    }

    const data = fileResult.value;
    document.title = `DataDock – ${data.filename}`;
    document.getElementById("lc-icon").textContent = fileIcon(data.filename);
    document.getElementById("lc-filename").textContent = data.filename;
    document.getElementById("lc-size").textContent = fmtBytes(data.file_size);
    document.getElementById("lc-date").textContent = fmtDate(data.completed_at);
    document.getElementById("lc-views").textContent = (data.views || 0).toLocaleString() + " views";
    document.getElementById("lc-downloads").textContent = (data.downloads || 0).toLocaleString() + " downloads";
    document.getElementById("lc-download-btn").href = `/api/f/${shareId}/download`;

    document.getElementById("lc-loading").hidden = true;
    document.getElementById("lc-card").hidden = false;
  }

  init();
})();
