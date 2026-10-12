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
