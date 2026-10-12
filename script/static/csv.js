/* CSV 表格页：已渲染行的纯前端搜索过滤。 */
(function () {
  var input = document.getElementById('csv-filter');
  var count = document.getElementById('csv-count');
  var table = document.getElementById('csv-table');
  if (!input || !table || !table.tBodies.length) return;

  var rows = Array.prototype.slice.call(table.tBodies[0].rows);
  var total = rows.length;

  function apply() {
    var q = (input.value || '').trim().toLowerCase();
    var shown = 0;
    rows.forEach(function (tr) {
      var hit = !q || tr.textContent.toLowerCase().indexOf(q) >= 0;
      tr.style.display = hit ? '' : 'none';
      if (hit) shown++;
    });
    if (count) {
      count.textContent = q ? (shown + ' / ' + total + ' 行') : (total + ' 行');
    }
  }

  input.addEventListener('input', apply);
  input.addEventListener('search', apply);
})();
