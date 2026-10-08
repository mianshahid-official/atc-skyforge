/**
 * ATC SkyForge - Flight Deck Web Application Controller
 * Handles SSE real-time telemetry, calibration sliders,
 * sample test runs, 50-video batch runs, and video playback.
 */

document.addEventListener('DOMContentLoaded', () => {
  // DOM Elements - Telemetry & Header
  const systemStatusBadge = document.getElementById('systemStatusBadge');
  const systemStatusText = document.getElementById('systemStatusText');
  const activeModeTag = document.getElementById('activeModeTag');
  const scriptsQueueCount = document.getElementById('scriptsQueueCount');
  const btnOpenFolder = document.getElementById('btnOpenFolder');

  // Sliders & Badges
  const pilotSpeedRange = document.getElementById('pilotSpeedRange');
  const pilotSpeedBadge = document.getElementById('pilotSpeedBadge');
  const towerSpeedRange = document.getElementById('towerSpeedRange');
  const towerSpeedBadge = document.getElementById('towerSpeedBadge');
  const convoSpeedRange = document.getElementById('convoSpeedRange');
  const convoSpeedBadge = document.getElementById('convoSpeedBadge');
  const bgVolumeRange = document.getElementById('bgVolumeRange');
  const bgVolumeBadge = document.getElementById('bgVolumeBadge');
  const voiceVolumeRange = document.getElementById('voiceVolumeRange');
  const voiceVolumeBadge = document.getElementById('voiceVolumeBadge');
  const openingDelayRange = document.getElementById('openingDelayRange');
  const openingDelayBadge = document.getElementById('openingDelayBadge');
  const btnResetSettings = document.getElementById('btnResetSettings');
  const presetButtons = document.querySelectorAll('.btn-preset');

  // Target Script & Action Buttons
  const sampleScriptSelect = document.getElementById('sampleScriptSelect');
  const btnTestRun = document.getElementById('btnTestRun');
  const btnRenderAll = document.getElementById('btnRenderAll');
  const btnStop = document.getElementById('btnStop');

  // Progress Displays
  const currentVideoTitle = document.getElementById('currentVideoTitle');
  const stepMessage = document.getElementById('stepMessage');
  const videoPercentText = document.getElementById('videoPercentText');
  const itemCounterText = document.getElementById('itemCounterText');
  const activeVideoProgressBar = document.getElementById('activeVideoProgressBar');
  const activeVideoPctVal = document.getElementById('activeVideoPctVal');
  const batchProgressSection = document.getElementById('batchProgressSection');
  const batchOverallProgressBar = document.getElementById('batchOverallProgressBar');
  const batchOverallPctVal = document.getElementById('batchOverallPctVal');

  // Pipeline Step Nodes
  const stepNodes = {
    tts: document.getElementById('stepNodeTts'),
    fx: document.getElementById('stepNodeFx'),
    shake: document.getElementById('stepNodeShake'),
    sub: document.getElementById('stepNodeSub'),
    mux: document.getElementById('stepNodeMux'),
  };

  // Console Logs
  const consoleLogs = document.getElementById('consoleLogs');
  const chkAutoScroll = document.getElementById('chkAutoScroll');
  const btnClearConsole = document.getElementById('btnClearConsole');

  // Video Preview & Gallery
  const previewPlayer = document.getElementById('previewPlayer');
  const previewSource = document.getElementById('previewSource');
  const videoPlaceholder = document.getElementById('videoPlaceholder');
  const previewBadge = document.getElementById('previewBadge');
  const previewInfoBar = document.getElementById('previewInfoBar');
  const previewVideoTitle = document.getElementById('previewVideoTitle');
  const previewVideoMeta = document.getElementById('previewVideoMeta');
  const btnDownloadPreview = document.getElementById('btnDownloadPreview');
  const libraryList = document.getElementById('libraryList');
  const completedCount = document.getElementById('completedCount');
  const btnRefreshVideos = document.getElementById('btnRefreshVideos');

  // Internal State
  let previousLogs = new Set();
  let isRunning = false;
  let currentLoadedVideo = '';
  let updateSettingsTimeout = null;

  // Preset Configurations
  const PRESETS = {
    calibrated: {
      pilot_speed: 1.26,
      tower_speed: 1.26,
      conversation_speed: 1.26,
      bg_volume: 0.35,
      voice_volume: 1.6,
      opening_delay: 1.0,
    },
    emergency: {
      pilot_speed: 1.35,
      tower_speed: 1.26,
      conversation_speed: 1.15,
      bg_volume: 0.30,
      voice_volume: 1.7,
      opening_delay: 0.8,
    },
    clarity: {
      pilot_speed: 1.20,
      tower_speed: 1.18,
      conversation_speed: 1.05,
      bg_volume: 0.22,
      voice_volume: 1.8,
      opening_delay: 1.2,
    },
  };

  // ==============================================================================
  // 1. Settings Synchronization & Slider Events
  // ==============================================================================
  function syncSliderDisplays() {
    pilotSpeedBadge.textContent = `${parseFloat(pilotSpeedRange.value).toFixed(2)}x`;
    towerSpeedBadge.textContent = `${parseFloat(towerSpeedRange.value).toFixed(2)}x`;
    convoSpeedBadge.textContent = `${parseFloat(convoSpeedRange.value).toFixed(2)}x`;
    bgVolumeBadge.textContent = `${parseFloat(bgVolumeRange.value).toFixed(2)}`;
    voiceVolumeBadge.textContent = `${parseFloat(voiceVolumeRange.value).toFixed(1)}x`;
    openingDelayBadge.textContent = `${parseFloat(openingDelayRange.value).toFixed(1)}s`;
  }

  function getActiveSettingsPayload() {
    return {
      pilot_speed: parseFloat(pilotSpeedRange.value),
      tower_speed: parseFloat(towerSpeedRange.value),
      conversation_speed: parseFloat(convoSpeedRange.value),
      bg_volume: parseFloat(bgVolumeRange.value),
      voice_volume: parseFloat(voiceVolumeRange.value),
      opening_delay: parseFloat(openingDelayRange.value),
    };
  }

  function sendSettingsUpdate() {
    const payload = getActiveSettingsPayload();
    fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }).catch(err => console.warn('Settings update failed:', err));
  }

  function onSliderInput() {
    syncSliderDisplays();
    clearTimeout(updateSettingsTimeout);
    updateSettingsTimeout = setTimeout(sendSettingsUpdate, 250);
  }

  [pilotSpeedRange, towerSpeedRange, convoSpeedRange, bgVolumeRange, voiceVolumeRange, openingDelayRange].forEach(input => {
    input.addEventListener('input', onSliderInput);
  });

  // Presets Handlers
  presetButtons.forEach(btn => {
    btn.addEventListener('click', () => {
      const presetKey = btn.getAttribute('data-preset');
      const cfg = PRESETS[presetKey];
      if (!cfg) return;

      presetButtons.forEach(b => b.classList.remove('active'));
      btn.classList.add('active');

      pilotSpeedRange.value = cfg.pilot_speed;
      towerSpeedRange.value = cfg.tower_speed;
      convoSpeedRange.value = cfg.conversation_speed;
      bgVolumeRange.value = cfg.bg_volume;
      voiceVolumeRange.value = cfg.voice_volume;
      openingDelayRange.value = cfg.opening_delay;

      syncSliderDisplays();
      sendSettingsUpdate();
      addLogEntry(`[CONFIG] Applied preset: ${btn.textContent.trim()}`, 'log-info');
    });
  });

  btnResetSettings.addEventListener('click', () => {
    fetch('/api/reset_settings', { method: 'POST' })
      .then(res => res.json())
      .then(data => {
        if (data.settings) {
          pilotSpeedRange.value = data.settings.pilot_speed;
          towerSpeedRange.value = data.settings.tower_speed;
          convoSpeedRange.value = data.settings.conversation_speed;
          bgVolumeRange.value = data.settings.bg_volume;
          voiceVolumeRange.value = data.settings.voice_volume;
          openingDelayRange.value = data.settings.opening_delay;
          syncSliderDisplays();
          addLogEntry('[CONFIG] Reset settings to calibrated defaults.', 'log-info');
        }
      });
  });

  // ==============================================================================
  // 2. Action Launchers (Test Run, Render All, Stop, Explorer)
  // ==============================================================================
  btnTestRun.addEventListener('click', () => {
    if (isRunning) return;
    const selectedIdx = parseInt(sampleScriptSelect.value, 10) || 0;
    const selectedText = sampleScriptSelect.options[sampleScriptSelect.selectedIndex]?.text || `Script ${selectedIdx + 1}`;

    addLogEntry(`[USER] Initiating Test Run for: ${selectedText}...`, 'log-info');

    // Make sure latest slider settings are committed
    sendSettingsUpdate();

    fetch('/api/test_run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ script_index: selectedIdx }),
    })
      .then(res => res.json())
      .then(data => {
        if (data.status === 'started') {
          setEngineRunningState(true, 'test');
        } else {
          addLogEntry(`[ERROR] Test run failed to launch: ${JSON.stringify(data)}`, 'log-error');
        }
      })
      .catch(err => {
        addLogEntry(`[ERROR] Network error starting test run: ${err}`, 'log-error');
      });
  });

  btnRenderAll.addEventListener('click', () => {
    if (isRunning) return;
    const confirmBatch = confirm('Start Batch Render of all 50 emergency videos? You can pause or stop at any time.');
    if (!confirmBatch) return;

    addLogEntry('[USER] Initiating Full Batch Render (50 Videos)...', 'log-info');
    sendSettingsUpdate();

    fetch('/api/render_all', { method: 'POST' })
      .then(res => res.json())
      .then(data => {
        if (data.status === 'started') {
          setEngineRunningState(true, 'batch');
        } else {
          addLogEntry(`[ERROR] Batch render failed to launch: ${JSON.stringify(data)}`, 'log-error');
        }
      })
      .catch(err => {
        addLogEntry(`[ERROR] Network error starting batch render: ${err}`, 'log-error');
      });
  });

  btnStop.addEventListener('click', () => {
    btnStop.disabled = true;
    addLogEntry('[USER] Cancel / Stop requested. Cancelling generation and removing incomplete video...', 'log-warn');
    setEngineRunningState(false, 'idle');
    fetch('/api/stop', { method: 'POST' })
      .then(res => res.json())
      .then(data => {
        addLogEntry('[INFO] Pipeline stopped. Incomplete files removed. Readjust settings as needed and click Render Videos again.', 'log-success');
        loadScriptsCatalog();
        refreshVideosList();
      })
      .catch(err => {
        console.warn('Stop failed:', err);
      });
  });

  btnOpenFolder.addEventListener('click', () => {
    fetch('/api/open_folder', { method: 'POST' })
      .then(() => addLogEntry('[SYSTEM] Opening rendered_videos folder in Windows Explorer.', 'log-system'));
  });

  // ==============================================================================
  // 3. UI State Transitions & Stepper
  // ==============================================================================
  function setEngineRunningState(running, mode = 'idle') {
    isRunning = running;
    btnTestRun.disabled = running;
    btnRenderAll.disabled = running;
    btnStop.disabled = !running;

    if (running) {
      systemStatusBadge.className = 'telemetry-value status-badge active-busy';
      systemStatusText.textContent = mode === 'test' ? 'TEST RUN ACTIVE' : 'BATCH RENDERING';
      activeModeTag.textContent = mode === 'test' ? 'TEST MODE' : 'BATCH MODE';
      activeModeTag.style.color = mode === 'test' ? 'var(--accent-amber)' : 'var(--accent-emerald)';
      activeModeTag.style.borderColor = mode === 'test' ? 'var(--accent-amber)' : 'var(--accent-emerald)';
    } else {
      systemStatusBadge.className = 'telemetry-value status-badge';
      systemStatusText.textContent = 'STANDBY READY';
      activeModeTag.textContent = 'IDLE';
      activeModeTag.style.color = 'var(--accent-cyan)';
      activeModeTag.style.borderColor = 'rgba(56, 189, 248, 0.3)';
    }
  }

  function updatePipelineStepper(step) {
    // Reset all step nodes
    Object.values(stepNodes).forEach(node => {
      node.classList.remove('active-step', 'done-step');
    });

    const stepOrder = ['tts', 'fx', 'shake', 'sub', 'mux'];
    let activeKey = null;

    if (step.includes('tts') || step.includes('synth') || step.includes('voice')) {
      activeKey = 'tts';
    } else if (step.includes('fx') || step.includes('radio') || step.includes('filter') || step.includes('static')) {
      activeKey = 'fx';
    } else if (step.includes('shake') || step.includes('crop') || step.includes('camera')) {
      activeKey = 'shake';
    } else if (step.includes('sub') || step.includes('ass') || step.includes('caption')) {
      activeKey = 'sub';
    } else if (step.includes('mux') || step.includes('encode') || step.includes('final') || step.includes('merg')) {
      activeKey = 'mux';
    } else if (step === 'done') {
      stepOrder.forEach(k => stepNodes[k].classList.add('done-step'));
      return;
    }

    if (activeKey) {
      const activeIdx = stepOrder.indexOf(activeKey);
      stepOrder.forEach((k, idx) => {
        if (idx < activeIdx) {
          stepNodes[k].classList.add('done-step');
        } else if (idx === activeIdx) {
          stepNodes[k].classList.add('active-step');
        }
      });
    }
  }

  // ==============================================================================
  // 4. Real-time Telemetry (SSE & Polling)
  // ==============================================================================
  function handleTelemetryPayload(payload) {
    if (!payload || !payload.status) return;
    const st = payload.status;

    // Running state
    setEngineRunningState(st.is_running, st.mode);

    // Current target titles
    if (st.current_title) {
      currentVideoTitle.textContent = `${st.current_title}.mp4`;
    } else if (!st.is_running) {
      currentVideoTitle.textContent = 'No active render job';
    }

    if (st.step_message) {
      stepMessage.textContent = st.step_message;
    }

    // Percentage values
    const vPct = Math.round(st.video_percent || 0);
    const oPct = Math.round(st.overall_percent || 0);

    videoPercentText.textContent = `${vPct}%`;
    activeVideoPctVal.textContent = `${vPct}%`;
    activeVideoProgressBar.style.width = `${vPct}%`;

    // Item counter & Batch bar
    if (st.mode === 'batch') {
      batchProgressSection.style.display = 'flex';
      itemCounterText.textContent = `Video ${st.current_index || 0} of ${st.total_items || 50}`;
      batchOverallPctVal.textContent = `${oPct}%`;
      batchOverallProgressBar.style.width = `${oPct}%`;
    } else {
      batchProgressSection.style.display = 'none';
      itemCounterText.textContent = st.is_running ? 'Sample Test Run' : 'Video -/-';
    }

    // Stepper nodes
    if (st.step) {
      updatePipelineStepper(st.step);
    }

    // Process new logs
    if (Array.isArray(st.logs)) {
      st.logs.forEach(log => {
        if (!previousLogs.has(log)) {
          previousLogs.add(log);
          let cls = 'log-info';
          if (log.toLowerCase().includes('error') || log.toLowerCase().includes('failed')) cls = 'log-error';
          else if (log.toLowerCase().includes('complete') || log.toLowerCase().includes('success')) cls = 'log-success';
          else if (log.toLowerCase().includes('warning') || log.toLowerCase().includes('stop')) cls = 'log-warn';
          else if (log.includes('[') && log.includes('---')) cls = 'log-system';
          addLogEntry(log, cls);
        }
      });
    }

    // Autoload newly rendered video into preview
    if (st.last_rendered && st.last_rendered !== currentLoadedVideo) {
      loadVideoPreview(st.last_rendered);
      refreshVideosList();
    }
  }

  function addLogEntry(text, cssClass = 'log-info') {
    const line = document.createElement('div');
    line.className = `log-line ${cssClass}`;
    line.textContent = text;
    consoleLogs.appendChild(line);

    if (chkAutoScroll.checked) {
      consoleLogs.scrollTop = consoleLogs.scrollHeight;
    }
  }

  btnClearConsole.addEventListener('click', () => {
    consoleLogs.innerHTML = '';
    previousLogs.clear();
  });

  // Setup EventSource (Server-Sent Events)
  function connectSSE() {
    const sse = new EventSource('/api/events');

    sse.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data);
        handleTelemetryPayload(payload);
      } catch (err) {
        console.error('SSE JSON error:', err);
      }
    };

    sse.onerror = () => {
      console.warn('SSE connection interrupted. Reconnecting in 3s...');
      sse.close();
      setTimeout(connectSSE, 3000);
    };
  }

  // ==============================================================================
  // 5. Video Preview Player & Gallery
  // ==============================================================================
  function loadVideoPreview(filename) {
    if (!filename) return;
    currentLoadedVideo = filename;

    const streamUrl = `/videos/${encodeURIComponent(filename)}?t=${Date.now()}`;
    previewSource.src = streamUrl;
    previewPlayer.load();

    videoPlaceholder.style.display = 'none';
    previewPlayer.style.display = 'block';

    previewInfoBar.style.display = 'flex';
    previewVideoTitle.textContent = filename;
    previewVideoMeta.textContent = 'Rendered Video (9:16 vertical short)';
    btnDownloadPreview.href = streamUrl;
    btnDownloadPreview.download = filename;

    previewBadge.textContent = 'READY TO PLAY';
    previewBadge.style.color = 'var(--accent-emerald)';

    // Do not autoplay (user requested manual play only)
    previewPlayer.pause();

    addLogEntry(`[PLAYER] Loaded preview for: ${filename}`, 'log-success');
  }

  function refreshVideosList() {
    fetch('/api/videos')
      .then(res => res.json())
      .then(videos => {
        completedCount.textContent = videos.length;
        if (!videos.length) {
          libraryList.innerHTML = '<div class="library-empty">No rendered videos yet. Run a test sample!</div>';
          return;
        }

        libraryList.innerHTML = '';
        videos.forEach(v => {
          const item = document.createElement('div');
          item.className = `library-item ${v.filename === currentLoadedVideo ? 'active-item' : ''}`;
          item.innerHTML = `
            <div style="display: flex; flex-direction: column; gap: 2px;">
              <span class="lib-item-title">${v.filename}</span>
              <span class="lib-item-meta">${v.size_mb} MB &bull; ${v.modified}</span>
            </div>
            <button class="btn-text-small" style="font-size: 0.72rem; color: var(--accent-cyan);">Play ▶</button>
          `;
          item.addEventListener('click', () => {
            loadVideoPreview(v.filename);
            document.querySelectorAll('.library-item').forEach(el => el.classList.remove('active-item'));
            item.classList.add('active-item');
          });
          libraryList.appendChild(item);
        });

        // If no video is currently loaded in preview, load the newest one
        if (!currentLoadedVideo && videos.length > 0) {
          loadVideoPreview(videos[0].filename);
        }
      })
      .catch(err => console.warn('Failed to load video list:', err));
  }

  btnRefreshVideos.addEventListener('click', refreshVideosList);

  // ==============================================================================
  // 6. Scripts Catalog Loader
  // ==============================================================================
  function loadScriptsCatalog() {
    fetch('/api/scripts')
      .then(res => res.json())
      .then(data => {
        const scripts = Array.isArray(data) ? data : (data.scripts || []);
        if (!scripts.length) return;

        const renderedCount = data.total_rendered || scripts.filter(s => s.is_rendered).length;
        const totalCount = data.total_count || scripts.length;
        const remainingCount = totalCount - renderedCount;
        const firstUnrendered = (typeof data.first_unrendered_index === 'number') ? data.first_unrendered_index : 0;

        scriptsQueueCount.textContent = `${renderedCount}/${totalCount} RENDERED (${remainingCount} REMAINING)`;
        sampleScriptSelect.innerHTML = '';

        scripts.forEach(s => {
          const opt = document.createElement('option');
          opt.value = s.index;
          const statusIcon = s.is_rendered ? `✔ [RENDERED]` : `⏳ [PENDING]`;
          opt.textContent = `[#${s.index + 1}] ${s.title} - ${statusIcon}`;
          if (s.is_rendered) {
            opt.style.color = '#10b981';
          }
          sampleScriptSelect.appendChild(opt);
        });

        // Automatically preselect the first unrendered script!
        sampleScriptSelect.value = firstUnrendered;

        // Update test button subtext with the active target
        const btnSub = btnTestRun.querySelector('.btn-sub-text');
        if (btnSub && scripts[firstUnrendered]) {
          btnSub.textContent = `Target: Script #${firstUnrendered + 1} (${scripts[firstUnrendered].title})`;
        }
      })
      .catch(err => console.warn('Failed to load scripts:', err));
  }

  sampleScriptSelect.addEventListener('change', () => {
    const selectedIdx = parseInt(sampleScriptSelect.value, 10);
    const btnSub = btnTestRun.querySelector('.btn-sub-text');
    if (btnSub) {
      btnSub.textContent = `Target: Script #${selectedIdx + 1}`;
    }
  });

  // Initial Sync from server
  function initialLoad() {
    fetch('/api/status')
      .then(res => res.json())
      .then(data => {
        if (data.settings) {
          pilotSpeedRange.value = data.settings.pilot_speed || 1.28;
          towerSpeedRange.value = data.settings.tower_speed || 1.23;
          convoSpeedRange.value = data.settings.conversation_speed || 1.10;
          bgVolumeRange.value = data.settings.bg_volume || 0.35;
          voiceVolumeRange.value = data.settings.voice_volume || 1.6;
          openingDelayRange.value = data.settings.opening_delay || 1.0;
          syncSliderDisplays();
        }
        handleTelemetryPayload(data);
      })
      .catch(err => console.warn('Failed to fetch initial status:', err));

    loadScriptsCatalog();
    refreshVideosList();
    connectSSE();
  }

  initialLoad();
});
