/**
 * Claw-brain Web Console - Frontend
 * Modular vanilla JS with state management
 */

// ============================================
// State Management
// ============================================
const State = {
  running: false,
  loopCount: 0,
  pollTimer: null,
  chatOpen: false,
  hasQuestion: false,

  // Data stores
  brainLog: [],
  clawLog: [],
  chatMessages: [],

  // Config
  get goal() { return document.getElementById('goal-input').value; },
  get agent() { return document.getElementById('agent-select').value; },
  get maxLoops() { return parseInt(document.getElementById('max-loops').value) || 10; },
  get interval() { return parseInt(document.getElementById('loop-interval').value) || 15; },
};

// ============================================
// DOM Helpers
// ============================================
const $ = id => document.getElementById(id);
const $$ = sel => document.querySelectorAll(sel);

function escapeHtml(text) {
  if (!text) return '';
  return String(text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
    .replace(/\n/g, '<br>');
}

function showToast(message, duration = 5000) {
  const existing = document.querySelector('.toast');
  if (existing) existing.remove();

  const toast = document.createElement('div');
  toast.className = 'toast';
  toast.textContent = message;
  document.body.appendChild(toast);

  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transition = 'opacity 0.5s';
    setTimeout(() => toast.remove(), 600);
  }, duration);
}

// ============================================
// Panel Rendering
// ============================================
function renderBrainEntries() {
  const container = $('brain-body');
  if (!State.brainLog.length) {
    container.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">🧠</div>
        <div class="empty-text">AI 大脑等待启动</div>
        <div class="empty-hint">设定目标后点击「启动系统」</div>
      </div>`;
    return;
  }

  const entries = State.brainLog.slice().reverse();
  container.innerHTML = entries.map(e => {
    const statusColors = {
      continue: 'var(--success)',
      milestone: 'var(--warning)',
      blocked: 'var(--danger)',
      pause: 'var(--info)',
      need_input: 'var(--warning)'
    };
    const color = statusColors[e.status] || 'var(--text-muted)';

    let html = `<div class="entry">`;
    html += `<div class="entry-round">Round ${e.round} — Brain</div>`;

    if (e.thought) {
      html += `<div class="entry-label brain">思考</div>`;
      html += `<div class="entry-text">${escapeHtml(e.thought)}</div>`;
    }
    if (e.observation) {
      html += `<div class="entry-label brain">观察</div>`;
      html += `<div class="entry-text">${escapeHtml(e.observation)}</div>`;
    }
    if (e.action) {
      html += `<div class="action-box">`;
      html += `<div class="action-label">发送给小龙虾</div>`;
      html += `<div class="action-text">${escapeHtml(e.action)}</div>`;
      html += `</div>`;
    }
    if (e.update_memory) {
      html += `<div class="entry-text" style="color:var(--text-muted);font-style:italic">${escapeHtml(e.update_memory)}</div>`;
    }
    html += `<div style="margin-top:8px;font-size:11px;color:${color};font-weight:600">[${(e.status || '?').toUpperCase()}]</div>`;
    html += `</div>`;
    return html;
  }).join('');
}

function renderClawEntries() {
  const container = $('claw-body');
  if (!State.clawLog.length) {
    container.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">🦞</div>
        <div class="empty-text">小龙虾待命中</div>
        <div class="empty-hint">系统启动后将显示执行记录</div>
      </div>`;
    return;
  }

  const entries = State.clawLog.slice().reverse();
  container.innerHTML = entries.map(e => {
    const cls = e.success ? 'ok' : 'fail';
    const icon = e.success ? '✓' : '✗';

    let html = `<div class="entry claw">`;
    html += `<div class="entry-round">Round ${e.round} — OpenClaw [${icon}]</div>`;
    if (e.instruction) {
      html += `<div class="entry-label claw">指令</div>`;
      html += `<div class="entry-text">${escapeHtml(e.instruction)}</div>`;
    }
    if (e.result) {
      html += `<div class="entry-label claw">结果</div>`;
      html += `<div class="result-box result-${cls}">${escapeHtml(e.result.substring(0, 500))}</div>`;
    }
    html += `</div>`;
    return html;
  }).join('');
}

function renderMemory(data) {
  const container = $('memory-body');
  if (!data || (!data.current_strategy && !data.milestones?.length && !data.successful_patterns?.length && !data.failed_attempts?.length)) {
    container.innerHTML = `
      <div class="empty-state" style="padding:30px">
        <div class="empty-text">白板是空的</div>
      </div>`;
    return;
  }

  let html = '';

  if (data.current_strategy) {
    html += `<div class="mem-card">`;
    html += `<div class="mem-title">🎯 当前策略</div>`;
    html += `<div class="mem-body">${escapeHtml(data.current_strategy)}</div>`;
    html += `</div>`;
  }

  if (data.milestones?.length) {
    html += `<div class="mem-card">`;
    html += `<div class="mem-title">🌟 里程碑 (${data.milestones.length})</div>`;
    data.milestones.slice(-5).forEach(m => {
      html += `<div class="mem-body">${escapeHtml(m.description || '')}</div>`;
    });
    html += `</div>`;
  }

  if (data.successful_patterns?.length) {
    html += `<div class="mem-card">`;
    html += `<div class="mem-title" style="color:var(--success)">✅ 成功模式</div>`;
    html += `<div class="mem-body">${data.successful_patterns.slice(-5).map(escapeHtml).join('<br>')}</div>`;
    html += `</div>`;
  }

  if (data.failed_attempts?.length) {
    html += `<div class="mem-card">`;
    html += `<div class="mem-title" style="color:var(--danger)">❌ 失败记录</div>`;
    html += `<div class="mem-body">${data.failed_attempts.slice(-5).map(escapeHtml).join('<br>')}</div>`;
    html += `</div>`;
  }

  container.innerHTML = html;
}

function renderChatMessages() {
  const container = $('chat-body');
  if (!State.chatMessages.length) {
    container.innerHTML = '<div class="chat-empty">暂无对话</div>';
    return;
  }

  container.innerHTML = State.chatMessages.map(m => {
    const cls = m.role === 'usr' ? 'user' : 'system';
    const lbl = m.role === 'usr' ? '你' : '系统';
    return `<div class="chat-msg ${cls}">
      <div class="chat-msg-label">${lbl}</div>
      <div>${escapeHtml(m.text)}</div>
    </div>`;
  }).join('');
}

// ============================================
// API Communication
// ============================================
async function apiStart() {
  const res = await fetch('/api/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      goal: State.goal,
      agent: State.agent,
      max_loops: State.maxLoops,
      loop_interval: State.interval
    })
  });
  return res.json();
}

async function apiStop() {
  const res = await fetch('/api/stop', { method: 'POST' });
  return res.json();
}

async function apiAnswer(answer) {
  const res = await fetch('/api/answer', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ answer })
  });
  return res.json();
}

async function apiState() {
  const res = await fetch('/api/state');
  return res.json();
}

// ============================================
// Polling
// ============================================
async function poll() {
  try {
    const data = await apiState();

    // Update state
    State.loopCount = data.round || 0;
    State.running = data.status?.includes('运行') || false;
    State.hasQuestion = data.has_question || false;

    // Update brain log (append new entries)
    if (data.brain_log) {
      data.brain_log.forEach(entry => {
        if (!State.brainLog.find(e => e.round === entry.round && e.thought === entry.thought)) {
          State.brainLog.push(entry);
        }
      });
    }

    // Update claw log
    if (data.claw_log) {
      data.claw_log.forEach(entry => {
        if (!State.clawLog.find(e => e.round === entry.round && e.instruction === entry.instruction)) {
          State.clawLog.push(entry);
        }
      });
    }

    // Update chat
    if (data.chat_messages) {
      State.chatMessages = data.chat_messages;
    }

    // Render
    renderBrainEntries();
    renderClawEntries();
    if (data.memory_data) renderMemory(data.memory_data);
    renderChatMessages();

    // Update UI
    updateStatusUI(data.status);
    $('round-badge').textContent = `Round ${data.round || 0}`;
    $('claw-badge').textContent = `${data.claw_count || 0} 次执行`;
    $('footer-agent').textContent = State.agent;

    // Chat badge
    if (State.hasQuestion) {
      $('chat-badge').textContent = State.chatMessages.length;
      $('chat-badge').classList.remove('hidden');
      if (!State.chatOpen) toggleChat();
    } else {
      $('chat-badge').classList.add('hidden');
    }

    // Auto-scroll
    const bb = $('brain-body');
    const cb = $('claw-body');
    if (bb) bb.scrollTop = bb.scrollHeight;
    if (cb) cb.scrollTop = cb.scrollHeight;

  } catch (err) {
    console.error('Poll error:', err);
  }
}

function updateStatusUI(status) {
  const dot = $('system-dot');
  const text = $('system-status');

  if (!status) return;

  text.textContent = status;

  if (status.includes('运行')) {
    dot.className = 'dot dot-green';
  } else if (status.includes('思考')) {
    dot.className = 'dot dot-yellow';
  } else if (status.includes('停止') || status.includes('待命')) {
    dot.className = 'dot';
    if (State.running) {
      // Auto-detect stop
      State.running = false;
      updateStartButton();
      if (State.pollTimer) {
        clearInterval(State.pollTimer);
        State.pollTimer = null;
      }
    }
  }
}

function updateStartButton() {
  const btn = $('start-btn');
  if (State.running) {
    btn.textContent = '停止系统';
    btn.className = 'btn btn-danger';
  } else {
    btn.textContent = '启动系统';
    btn.className = 'btn btn-primary';
  }
}

// ============================================
// Event Handlers
// ============================================
async function handleStartStop() {
  const btn = $('start-btn');

  if (!State.running) {
    // Start
    if (!State.goal.trim()) {
      showToast('请输入目标');
      return;
    }

    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>启动中...';

    try {
      const data = await apiStart();
      if (data.error) {
        showToast(data.error);
        btn.disabled = false;
        btn.textContent = '启动系统';
        return;
      }

      State.running = true;
      State.brainLog = [];
      State.clawLog = [];
      updateStartButton();

      // Start polling
      if (State.pollTimer) clearInterval(State.pollTimer);
      State.pollTimer = setInterval(poll, 2000);

    } catch (err) {
      showToast('启动失败: ' + err.message);
      btn.disabled = false;
      btn.textContent = '启动系统';
    }

  } else {
    // Stop
    try {
      await apiStop();
      State.running = false;
      updateStartButton();
      if (State.pollTimer) {
        clearInterval(State.pollTimer);
        State.pollTimer = null;
      }
      poll();
    } catch (err) {
      showToast('停止失败: ' + err.message);
    }
  }
}

function handleTabSwitch(e) {
  const tab = e.target.closest('.tab');
  if (!tab) return;

  const panelId = tab.dataset.panel;

  // Update tabs
  $$('.tab').forEach(t => t.classList.remove('active'));
  tab.classList.add('active');

  // Update panels
  $$('.panel').forEach(p => p.classList.remove('active'));
  $(`panel-${panelId}`).classList.add('active');
}

function handleQuickGoal(e) {
  const btn = e.target.closest('.quick-btn');
  if (!btn) return;

  const goalMap = {
    '获客': '分析当前获客渠道，找到转化率最高的方式并持续放大，直到产生真实收入。',
    '定价': '调研 Gumroad 上卖 AI prompt 的最佳定价策略和竞品分析。',
    'Reddit': '搜索 Reddit 上关于 AI automation 赚钱的最新帖子，总结成功案例。'
  };

  const key = btn.dataset.goal;
  if (goalMap[key]) {
    $('goal-input').value = goalMap[key];
  }
}

function toggleChat() {
  State.chatOpen = !State.chatOpen;
  const panel = $('chat-panel');
  const btn = $('chat-toggle');

  panel.classList.toggle('hidden', !State.chatOpen);
  btn.textContent = State.chatOpen ? '✕' : '💬';

  if (State.chatOpen) {
    const body = $('chat-body');
    if (body) body.scrollTop = body.scrollHeight;
    $('chat-input').focus();
  }
}

async function handleChatSend() {
  const input = $('chat-input');
  const text = input.value.trim();
  if (!text) return;

  input.value = '';
  $('chat-send').disabled = true;

  try {
    await apiAnswer(text);
    await poll();
  } catch (err) {
    showToast('发送失败: ' + err.message);
  } finally {
    $('chat-send').disabled = false;
  }
}

// ============================================
// Initialization
// ============================================
function init() {
  // Event listeners
  $('start-btn').addEventListener('click', handleStartStop);
  $$('.tabs')[0].addEventListener('click', handleTabSwitch);
  $$('.quick-goals')[0].addEventListener('click', handleQuickGoal);

  // Chat
  $('chat-toggle').addEventListener('click', toggleChat);
  $('chat-close').addEventListener('click', toggleChat);
  $('chat-send').addEventListener('click', handleChatSend);
  $('chat-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') handleChatSend();
  });

  // Initial poll
  poll();
}

// Start when DOM is ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
