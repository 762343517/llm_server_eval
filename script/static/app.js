/* 任务页：轮询进度 + 表格过滤；结果区始终由服务端渲染（SSR）。 */
(function () {
  var runId = window.RUN_ID;
  if (!runId) return;

  var $ = function (id) { return document.getElementById(id); };

  function setText(id, v) { var el = $(id); if (el) el.textContent = v; }

  function render(s) {
    var p = s.progress || {};
    if ($('bar-fill')) $('bar-fill').style.width = (p.pct || 0) + '%';
    setText('bar-text', p.text || '');
    setText('stage-title', p.stage || '');
    setText('gen-done', s.gen.done);
    setText('gen-todo', s.gen.todo);
    setText('gen-tps', s.gen.tps);
    setText('judge-done', s.judge.done);
    setText('judge-total', s.judge.total);
    setText('err-count', (s.gen.errors || 0) + (s.judge.errors || 0));
    setText('cached-valid', s.gen.cached_valid);

    var badge = $('status-badge');
    if (badge) {
      badge.textContent = s.status_text || s.status;
      badge.className = 'badge ' + s.status;
    }
    var el = $('elapsed');
    if (el) el.innerHTML = '已运行 <b>' + (s.duration_s / 60).toFixed(1) + '</b> 分钟';

    if (s.logs) {
      var box = $('logs');
      if (box) {
        var atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
        box.textContent = s.logs.map(function (l) {
          return '[' + l.t + '] ' + l.msg;
        }).join('\n');
        if (atBottom) box.scrollTop = box.scrollHeight;
      }
    }
  }

  var finished = false;
  function poll() {
    fetch('/api/runs/' + runId + '/status', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (s) {
        render(s);
        if (s.status !== 'running') {
          if (!finished) {
            finished = true;
            setTimeout(function () { location.reload(); }, 700);
          }
          return;
        }
        setTimeout(poll, 2000);
      })
      .catch(function () { setTimeout(poll, 4000); });
  }

  if (window.RUN_STATUS === 'running') setTimeout(poll, 1200);

  window.stopRun = function () {
    if (!confirm('确认停止当前评测？已完成的题目会保留，可稍后续跑。')) return;
    fetch('/api/runs/' + runId + '/stop', { method: 'POST' })
      .then(function () { poll(); });
  };

  // ---------------- 逐题表格过滤（前端，数据来自 SSR 渲染的 DOM）
  var fi = $('filter-input'), fg = $('filter-grade'), fc = $('filter-count');
  function applyFilter() {
    var q = (fi && fi.value || '').toLowerCase();
    var g = fg && fg.value || '';
    var rows = document.querySelectorAll('#items-table tbody tr');
    var shown = 0;
    Array.prototype.forEach.call(rows, function (tr) {
      var okG = !g || tr.getAttribute('data-grade') === g;
      var okQ = !q || tr.textContent.toLowerCase().indexOf(q) >= 0;
      var show = okG && okQ;
      tr.style.display = show ? '' : 'none';
      if (show) shown++;
    });
    if (fc) fc.textContent = shown + ' 行';
  }
  if (fi) fi.addEventListener('input', applyFilter);
  if (fg) fg.addEventListener('change', applyFilter);
})();
