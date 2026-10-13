/* 首页表单草稿：未提交的内容写入 localStorage，返回首页时自动还原。
   服务端已记住「上次提交的配置」（runs/.last_config.json），因此这里只在
   草稿比上次提交更新（即用户提交后又改过、或根本没提交）时才覆盖表单值。 */
(function () {
  var form = document.querySelector('form.grid');
  if (!form || !window.localStorage) return;

  var KEY = 'zhipuEval.formDraft';
  var serverTs = Number(window.SAVED_CFG_AT || 0);
  var fields = Array.prototype.slice.call(form.elements).filter(function (el) {
    return el.name && el.type !== 'submit' && el.type !== 'button';
  });
  if (!fields.length) return;

  function snapshot() {
    var o = {};
    fields.forEach(function (el) { o[el.name] = el.value; });
    return o;
  }

  function save() {
    try {
      localStorage.setItem(KEY, JSON.stringify({
        at: Date.now() / 1000,
        values: snapshot()
      }));
    } catch (e) { /* 隐私模式等场景忽略 */ }
  }

  var draft = null;
  try { draft = JSON.parse(localStorage.getItem(KEY) || 'null'); } catch (e) { draft = null; }
  if (draft && draft.values && Number(draft.at) > serverTs) {
    fields.forEach(function (el) {
      if (Object.prototype.hasOwnProperty.call(draft.values, el.name)) {
        el.value = draft.values[el.name];
      }
    });
  }

  fields.forEach(function (el) {
    el.addEventListener('change', save);
    el.addEventListener('input', save);
  });
})();

/* 删除任务：用 fetch 提交并就地移除该行。
   早期实现是普通表单 POST，服务端 303 跳回首页会整页重载——滚动位置丢失
   就会「跳回页面顶部」；同时重载拿到的还是删除瞬间的快照。 */
(function () {
  var table = document.getElementById('runs-table');
  if (!table || !window.fetch) return;

  var wrap = document.getElementById('runs-table-wrap');
  var empty = document.getElementById('runs-empty');
  var countEl = document.getElementById('runs-count');

  function refreshFooter() {
    var n = table.tBodies[0] ? table.tBodies[0].rows.length : 0;
    if (countEl) countEl.textContent = '共 ' + n + ' 个任务';
    if (!n && wrap && empty) {
      wrap.hidden = true;
      empty.hidden = false;
    }
  }

  table.addEventListener('submit', function (ev) {
    var form = ev.target;
    if (!form || !form.classList || !form.classList.contains('js-delete-run')) return;
    ev.preventDefault();

    var runId = form.getAttribute('data-run-id') || '';
    if (!confirm('确认删除任务 ' + runId + ' 及其结果文件？')) return;

    var btn = form.querySelector('button');
    if (btn) btn.disabled = true;

    fetch(form.action, {
      method: 'POST',
      headers: { 'Accept': 'application/json' },
      cache: 'no-store'
    }).then(function (resp) {
      // 404 说明别的标签页已经删过了，同样按删除成功处理
      if (!resp.ok && resp.status !== 404) throw new Error('HTTP ' + resp.status);
      var row = table.querySelector('tr[data-run-id="' + runId + '"]');
      if (row && row.parentNode) row.parentNode.removeChild(row);
      refreshFooter();
    }).catch(function (err) {
      if (btn) btn.disabled = false;
      alert('删除失败：' + err.message);
    });
  });
})();
