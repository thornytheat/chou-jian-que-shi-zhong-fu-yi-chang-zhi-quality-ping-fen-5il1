// 原生 JS：无框架、无打包器。工作台执行剖析并轮询进度；复核页保存权重与重算。
(function () {
  function qs(sel, root) { return (root || document).querySelector(sel); }
  function qsa(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  // 页面内分段切换（v3：.segmented 或旧 .tabs；同一 .panel 内的 .tab-panel 互斥显示）
  qsa('[data-tab-target]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var group = btn.closest('.segmented, .tabs');
      if (group) qsa('button', group).forEach(function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
      var target = document.getElementById(btn.getAttribute('data-tab-target'));
      if (!target) return;
      var host = btn.closest('.panel') || document;
      qsa('.tab-panel', host).forEach(function (panel) {
        panel.hidden = (panel !== target);
      });
    });
  });

  async function readJSON(res) {
    var data = null;
    try { data = await res.json(); } catch (e) { data = { detail: '响应不是 JSON' }; }
    if (!res.ok) {
      var msg = (data && (data.detail || data.message)) || ('请求失败：' + res.status);
      throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
    }
    return data;
  }

  window.postJSON = async function (url, payload) {
    var res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload || {})
    });
    return readJSON(res);
  };

  function showError(err) {
    var box = qs('#error-box');
    if (!box) { window.alert('执行失败：' + err.message); return; }
    box.hidden = false;
    box.textContent = '执行失败：' + err.message + '（可修正后重试，无需刷新页面）';
    box.scrollIntoView({ block: 'center' });
  }

  function clearError() {
    var box = qs('#error-box');
    if (box) { box.hidden = true; box.textContent = ''; }
  }

  // ---------- 工作台：执行 + 进度 ----------
  var progressCard = qs('#progress-card');
  var bar = qs('#progress-bar');
  var text = qs('#progress-text');
  var stageList = qs('#stage-list');
  var buttons = qsa('#run-sample, #run-text, #upload-form button[type="submit"], button[form="upload-form"]');
  var stateTag = qs('#progress-state');

  function setBusy(busy) {
    buttons.forEach(function (b) { b.disabled = busy; });
  }

  function renderStages(index) {
    if (!stageList) return;
    qsa('li', stageList).forEach(function (li) {
      var i = parseInt(li.getAttribute('data-stage'), 10);
      li.className = i < index ? 'done' : (i === index ? 'active' : '');
    });
  }

  async function poll(runId) {
    var total = 7;
    for (var i = 0; i < 600; i++) {
      var st = await fetch('/api/runs/' + runId + '/status').then(readJSON);
      total = st.stage_total || total;
      var done = st.status === 'done';
      var failed = st.status === 'failed';
      if (progressCard) progressCard.hidden = false;
      if (bar) bar.style.width = (failed ? 100 : Math.min(100, Math.round(((st.stage_index + (done ? 1 : 0)) / total) * 100))) + '%';
      if (text) {
        text.textContent = failed
          ? ('执行失败：' + (st.error || '未知错误'))
          : (done ? ('完成：' + st.rows + ' 行，总分 ' + Number(st.total_score).toFixed(2) + '（' + st.grade + '），正在跳转结果页…')
                  : ('当前阶段：' + st.stage + '（' + (st.stage_index + 1) + '/' + total + '）'));
      }
      renderStages(done ? total : st.stage_index);
      if (stateTag) stateTag.textContent = failed ? '失败' : (done ? '已完成' : '执行中 ' + (st.stage_index + 1) + '/' + total);
      if (failed) { setBusy(false); showError(new Error(st.error || '计算失败')); return; }
      if (done) { window.location.href = st.result_url; return; }
      await new Promise(function (r) { setTimeout(r, 300); });
    }
    setBusy(false);
    showError(new Error('等待超时，请到「复核与工单」页查看运行记录'));
  }

  function start(promise) {
    clearError();
    setBusy(true);
    if (progressCard) {
      progressCard.hidden = false;
      if (bar) bar.style.width = '4%';
      if (text) text.textContent = '已提交，正在载入数据…';
      renderStages(0);
      progressCard.scrollIntoView({ block: 'center' });
    }
    promise.then(function (data) { return poll(data.run_id); })
      .catch(function (err) { setBusy(false); showError(err); });
  }

  var sampleBtn = qs('#run-sample');
  if (sampleBtn) {
    sampleBtn.addEventListener('click', function () {
      start(window.postJSON(sampleBtn.getAttribute('data-endpoint'), { source: 'sample' }));
    });
  }

  var uploadForm = qs('#upload-form');
  if (uploadForm) {
    uploadForm.addEventListener('submit', function (ev) {
      ev.preventDefault();
      var input = qs('#file');
      if (!input || !input.files || !input.files.length) {
        showError(new Error('请先选择一个 CSV 文件（或改用粘贴文本方式）'));
        return;
      }
      var form = new FormData();
      form.append('file', input.files[0]);
      clearError();
      setBusy(true);
      if (progressCard) {
        progressCard.hidden = false;
        if (bar) bar.style.width = '4%';
        if (text) text.textContent = '正在上传并解析…';
      }
      start(fetch(uploadForm.getAttribute('data-endpoint'), { method: 'POST', body: form }).then(readJSON));
    });
  }

  var textBtn = qs('#run-text');
  if (textBtn) {
    textBtn.addEventListener('click', function () {
      var area = qs('#csv-text');
      var content = area ? area.value : '';
      if (!content.trim()) { showError(new Error('请先粘贴含表头的 CSV 文本')); return; }
      start(window.postJSON(textBtn.getAttribute('data-endpoint'), { csv_text: content }));
    });
  }

  // ---------- 复核页：权重保存 / 恢复默认 / 重算 ----------
  function weightMsg(message, cls) {
    var box = qs('#weight-msg');
    if (!box) return;
    box.hidden = false;
    box.className = 'alert ' + (cls || 'alert-info');
    box.innerHTML = message;
  }

  var weightForm = qs('#weight-form');
  if (weightForm) {
    weightForm.addEventListener('submit', function (ev) {
      ev.preventDefault();
      var columns = {};
      var ruleWeights = {};
      qsa('input[type="number"]', weightForm).forEach(function (input) {
        var name = input.getAttribute('name') || '';
        var value = parseFloat(input.value);
        if (isNaN(value)) return;
        if (name.indexOf('col:') === 0) columns[name.slice(4)] = value;
        if (name.indexOf('rule:') === 0) ruleWeights[name.slice(5)] = value;
      });
      window.postJSON(weightForm.getAttribute('data-endpoint'), { columns: columns, rule_weights: ruleWeights })
        .then(function (data) {
          weightMsg('权重已保存：列权重合计 ' + Object.keys(data.columns).length + ' 项，规则权重 ' +
            Object.keys(data.rule_weights).length + ' 项。点「按当前权重重算」看新分数。', 'alert-info');
        })
        .catch(function (err) { weightMsg('保存失败：' + err.message, 'alert-error'); });
    });
  }

  var resetBtn = qs('#reset-weights');
  if (resetBtn) {
    resetBtn.addEventListener('click', function () {
      window.postJSON(resetBtn.getAttribute('data-endpoint'), {})
        .then(function () { window.location.reload(); })
        .catch(function (err) { weightMsg('恢复失败：' + err.message, 'alert-error'); });
    });
  }

  var rescoreBtn = qs('#rescore');
  if (rescoreBtn) {
    rescoreBtn.addEventListener('click', function () {
      rescoreBtn.disabled = true;
      window.postJSON('/api/runs/' + rescoreBtn.getAttribute('data-run') + '/rescore', {})
        .then(function (data) {
          rescoreBtn.disabled = false;
          var link = '/result/' + data.run_id;
          weightMsg('已按当前权重重算：总分 <strong>' + Number(data.total).toFixed(2) + '</strong>，分级 <strong>' +
            data.grade + '</strong>。 <a href="' + link + '">打开结果页</a>', 'alert-info');
        })
        .catch(function (err) {
          rescoreBtn.disabled = false;
          weightMsg('重算失败：' + err.message, 'alert-error');
        });
    });
  }

  // ---------- 工作台：规则版本与批量重评 ----------
  (function reevalPanel() {
    var panel = qs('#reeval');
    if (!panel) return;
    var msgBox = qs('#reeval-msg');
    var bar = qs('#reeval-progress-bar');
    var text = qs('#reeval-progress-text');

    function msg(message, cls) {
      if (!msgBox) return;
      msgBox.hidden = false;
      msgBox.className = 'alert ' + (cls || 'alert-info');
      msgBox.innerHTML = message;
    }

    async function pollCampaign(campaignId) {
      for (var i = 0; i < 600; i++) {
        try {
          var c = await fetch('/api/reevals/' + campaignId).then(readJSON);
          if (bar && c.total) bar.style.width = Math.round((c.done_count / c.total) * 100) + '%';
          if (text) text.textContent = '进度 ' + c.done_count + '/' + c.total;
          if (c.status === 'done') {
            msg('重评全部完成：' + c.total + ' 批已整批切换到 v' + c.to_version +
              '，整改依据已更新。正在刷新…');
            window.setTimeout(function () { window.location.reload(); }, 900);
            return;
          }
          if (c.status === 'failed') {
            msg('重评中断：' + (c.error || '未知错误') + '。已完成的批次保留记录，' +
              '所有批次仍按旧版本作为整改依据。正在刷新…', 'alert-error');
            window.setTimeout(function () { window.location.reload(); }, 1200);
            return;
          }
        } catch (e) { /* 网络抖动时继续轮询 */ }
        await new Promise(function (r) { setTimeout(r, 800); });
      }
    }

    var startBtn = qs('#reeval-start');
    if (startBtn) {
      startBtn.addEventListener('click', function () {
        startBtn.disabled = true;
        window.postJSON('/api/reevals', {})
          .then(function (c) {
            msg('重评批次 ' + c.campaign_id + ' 已启动：' + c.total + ' 批 → v' + c.to_version + '。');
            pollCampaign(c.campaign_id);
          })
          .catch(function (err) {
            startBtn.disabled = false;
            msg('发起失败：' + err.message, 'alert-error');
          });
      });
    }

    var resumeBtn = qs('#reeval-resume');
    if (resumeBtn) {
      resumeBtn.addEventListener('click', function () {
        resumeBtn.disabled = true;
        var id = resumeBtn.getAttribute('data-campaign');
        window.postJSON('/api/reevals/' + id + '/resume', {})
          .then(function () {
            msg('已从断点续跑：已完成的批次跳过，同一批重试不会新增重复记录。');
            pollCampaign(id);
          })
          .catch(function (err) {
            resumeBtn.disabled = false;
            msg('续跑失败：' + err.message, 'alert-error');
          });
      });
    }

    var submitBtn = qs('#rules-version-submit');
    if (submitBtn) {
      submitBtn.addEventListener('click', function () {
        var area = qs('#rules-json');
        var note = qs('#rules-note');
        var content = area ? area.value.trim() : '';
        if (!content) { msg('请先粘贴规则集 JSON（version 必须未登记过）。', 'alert-error'); return; }
        var parsed;
        try { parsed = JSON.parse(content); }
        catch (e) { msg('JSON 解析失败：' + e.message, 'alert-error'); return; }
        submitBtn.disabled = true;
        window.postJSON('/api/rules/versions', { rules: parsed, note: note ? note.value : '' })
          .then(function (data) {
            msg('规则版本 v' + data.version + ' 已登记并设为当前版本；' + data.eligible +
              ' 个历史批次可发起批量重评。正在刷新…');
            window.setTimeout(function () { window.location.reload(); }, 900);
          })
          .catch(function (err) {
            submitBtn.disabled = false;
            msg('登记失败：' + err.message, 'alert-error');
          });
      });
    }

    // 页面打开时若有进行中的批次，直接接着轮询
    var runningId = panel.getAttribute('data-running');
    if (runningId) pollCampaign(runningId);
  })();

  // ---------- 复核页：历史运行搜索 / 统计 / 导出清单（纯前端增强，服务端查询不变） ----------
  (function reviewTools() {
    var table = qs('#history-table');
    if (!table) return;
    var searchInput = qs('#review-search');
    var stat = qs('#review-stat');

    function dataRows() {
      return qsa('tbody tr', table).filter(function (tr) { return tr.children.length > 1; });
    }
    function visibleRows() {
      return dataRows().filter(function (tr) { return !tr.hidden; });
    }
    function refreshStat() {
      if (!stat) return;
      var total = dataRows().length;
      var shown = visibleRows().length;
      stat.textContent = shown === total
        ? '共 ' + total + ' 次运行'
        : '筛选命中 ' + shown + ' / ' + total + ' 次运行';
    }
    function applyFilter() {
      var q = searchInput ? searchInput.value.trim().toLowerCase() : '';
      dataRows().forEach(function (tr) {
        tr.hidden = !!q && tr.textContent.toLowerCase().indexOf(q) === -1;
      });
      refreshStat();
    }
    if (searchInput) {
      searchInput.addEventListener('input', applyFilter);
      searchInput.addEventListener('search', applyFilter);
    }
    refreshStat();

    function cellText(td) {
      var text = (td.textContent || '').replace(/\s+/g, ' ').trim();
      return '"' + text.replace(/"/g, '""') + '"';
    }
    function exportCsv() {
      var headers = qsa('thead th', table).map(function (th) {
        return '"' + (th.textContent || '').replace(/\s+/g, ' ').trim().replace(/"/g, '""') + '"';
      });
      var lines = [headers.join(',')];
      visibleRows().forEach(function (tr) {
        lines.push(qsa('td', tr).map(cellText).join(','));
      });
      var blob = new Blob(['\ufeff' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' });
      var url = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = url;
      a.download = 'qualiscan-runs.csv';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    }
    qsa('[data-export-csv]').forEach(function (btn) {
      btn.addEventListener('click', exportCsv);
    });
  })();
})();
