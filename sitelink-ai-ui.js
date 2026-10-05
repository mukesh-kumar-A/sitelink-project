/* Dedicated evidence-grounded Ask SiteLink UI; read-only by design. */
(function () {
  'use strict';
  var api = window.SiteLinkControl;
  if (!api) return;
  var pendingActivityId = '';
  var cachedProject = '';
  var cachedAreas = [];
  var escapeHtml = api.escape;

  function renderResult(result) {
    var factRows = (result.facts || []).map(function (fact) {
      return '<article class="ai-fact"><small>' + escapeHtml(fact.label || fact.classification || 'Fact') + '</small><strong>' + escapeHtml(fact.value || '') + '</strong><span>' + escapeHtml(fact.basis || '') + '</span></article>';
    }).join('');
    var evidence = (result.evidence || []).map(function (item) {
      return '<article class="ai-evidence"><div><strong>' + escapeHtml(item.label || item.record_id) + '</strong><small>' + escapeHtml(item.type || 'approved source') + ' · ' + escapeHtml(item.activity_id || 'Project level') + ' · ' + escapeHtml(item.date || 'date not recorded') + '</small></div><blockquote>“' + escapeHtml(item.cited_quote || item.quote || '') + '”</blockquote></article>';
    }).join('');
    var inferences = (result.inferences || []).map(function (row) {
      return '<li>' + escapeHtml(row.text || '') + (row.basis ? '<small>' + escapeHtml(row.basis) + '</small>' : '') + '</li>';
    }).join('');
    var list = function (rows) { return (rows || []).map(function (value) { return '<li>' + escapeHtml(typeof value === 'string' ? value : JSON.stringify(value)) + '</li>'; }).join(''); };
    var activities = (result.affected_activities || []).map(function (row) {
      return '<li><strong>' + escapeHtml(row.activity_id || '') + ' · ' + escapeHtml(row.name || '') + '</strong><small>' + escapeHtml(row.classification || 'Inference') + ' · ' + escapeHtml((row.path || []).join(' → ')) + '</small></li>';
    }).join('');
    var answer = '<div class="ai-answer-head"><span class="role-badge">' + escapeHtml(result.intent || 'PROJECT QUESTION') + '</span><span class="ai-provider">' + escapeHtml(result.provider || 'Local deterministic engine') + (result.provider_fallback ? ' · fallback' : ' · validated citations') + '</span></div>' +
      '<p class="ai-answer-summary">' + escapeHtml(result.answer || 'No answer available.') + '</p>' +
      (result.ai_explanation ? '<div class="ai-model-explanation"><strong>AI-assisted explanation</strong><p>' + escapeHtml(result.ai_explanation) + '</p></div>' : '<div class="notice-box">Local deterministic answer is active. Configure the optional server-side AI provider to add a cited explanation.</div>') +
      (factRows ? '<h3>Facts and calculations</h3><div class="ai-facts">' + factRows + '</div>' : '') +
      '<h3>Evidence (' + Number(result.record_count || 0) + ' source records)</h3><div class="ai-evidence-list">' + (evidence || '<p class="field-hint">No approved source text was available to cite. Unreviewed reports do not support this answer.</p>') + '</div>' +
      (inferences ? '<h3>Inferences</h3><ul class="ai-list">' + inferences + '</ul>' : '') +
      (activities ? '<h3>Possible schedule exposure</h3><ul class="ai-list">' + activities + '</ul>' : '') +
      ((result.unknowns || []).length ? '<h3>Unknown or unavailable</h3><ul class="ai-list ai-unknown">' + list(result.unknowns) + '</ul>' : '') +
      ((result.assumptions || []).length ? '<h3>Assumptions</h3><ul class="ai-list">' + list(result.assumptions) + '</ul>' : '') +
      ((result.recommended_actions || []).length ? '<h3>Saved planner proposals</h3><ul class="ai-list">' + list(result.recommended_actions.map(function (r) { return r.recommendation + ' — ' + r.reason + (r.requires_approval ? ' (requires planner approval)' : ''); })) + '</ul>' : '') +
      '<div class="ai-answer-foot">' + escapeHtml(result.confidence || 'Low') + ' evidence coverage · ' + escapeHtml(result.confidence_method || '') + ' · ' + escapeHtml(result.intent_confidence || '') + ' · read-only; no schedule or approval changed</div>';
    return '<section class="panel ai-response">' + answer + '</section>';
  }

  function render() {
    var state = api.getState() || {};
    var activities = state.activities || [];
    var activityOptions = activities.map(function (activity) {
      return '<option value="' + escapeHtml(activity.id) + '"' + (activity.id === pendingActivityId ? ' selected' : '') + '>' + escapeHtml(activity.id + ' · ' + activity.name) + '</option>';
    }).join('');
    var areaOptions = cachedAreas.map(function (area) {
      return '<option value="' + escapeHtml(area.id) + '">' + escapeHtml((area.code ? area.code + ' · ' : '') + area.name + ' (' + area.level + ')') + '</option>';
    }).join('');
    return '<section class="ai-workspace"><section class="panel ai-ask-panel"><div class="panel-header"><div class="panel-heading"><h2>Ask SiteLink about this project</h2><p>Answers use this project’s schedule calculations and reviewed source records. Ask about a specific activity or investigate an area.</p></div><span class="role-badge">Read only</span></div>' +
      '<form id="sitelinkAIForm" class="ai-form"><label class="field-label" for="sitelinkAIQuestion">Your question</label><textarea class="textarea ai-question" id="sitelinkAIQuestion" maxlength="1200" required placeholder="Why is PIP-L6-042 behind, and which approved records support the answer?"></textarea>' +
      '<div class="ai-scope-controls"><label class="field-label">Activity scope<select class="select" id="sitelinkAIActivity"><option value="">Whole project</option>' + activityOptions + '</select></label><label class="field-label">Area investigation (optional)<select class="select" id="sitelinkAIArea"><option value="">No area filter</option>' + areaOptions + '</select></label><button class="button button-primary" id="sitelinkAISubmit" type="submit">Ask SiteLink</button></div>' +
      '<div class="ai-prompt-chips"><button type="button" data-ai-prompt="What are the recorded blockers and their source evidence?">Recorded blockers</button><button type="button" data-ai-prompt="Which activities are on the calculated critical path?">Critical path</button><button type="button" data-ai-prompt="What does the approved execution history say about this activity?">Execution history</button></div>' +
      '<p class="source-note">AI explanations are optional and server configured. Heuristic intent labels are not calibrated. A source document cannot issue instructions to the assistant. SiteLink will label missing facts and will not approve work or change the schedule.</p></form></section>' +
      '<div id="sitelinkAIStatus" class="ai-status-line">Loading project areas…</div><div id="sitelinkAIResult" aria-live="polite"></div></section>';
  }

  function bind(root) {
    var projectId = api.getProjectId();
    var areaSelect = root.querySelector('#sitelinkAIArea');
    if (projectId !== cachedProject) {
      cachedProject = projectId;
      cachedAreas = [];
      api.api('/api/project-control?projectId=' + encodeURIComponent(projectId), 'GET').then(function (control) {
        if (cachedProject !== projectId) return;
        cachedAreas = control.areas || [];
        if (areaSelect && areaSelect.isConnected) {
          var selected = areaSelect.value;
          areaSelect.innerHTML = '<option value="">No area filter</option>' + cachedAreas.map(function (area) {
            return '<option value="' + escapeHtml(area.id) + '">' + escapeHtml((area.code ? area.code + ' · ' : '') + area.name + ' (' + area.level + ')') + '</option>';
          }).join('');
          areaSelect.value = selected;
        }
        var status = root.querySelector('#sitelinkAIStatus');
        if (status) status.textContent = cachedAreas.length + ' project area(s) available. Answers are read-only.';
      }).catch(function () {
        var status = root.querySelector('#sitelinkAIStatus');
        if (status) status.textContent = 'Area options are unavailable; project and activity questions remain available.';
      });
    } else {
      var status = root.querySelector('#sitelinkAIStatus');
      if (status) status.textContent = cachedAreas.length + ' project area(s) available. Answers are read-only.';
    }
    var form = root.querySelector('#sitelinkAIForm');
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      var question = root.querySelector('#sitelinkAIQuestion').value.trim();
      var activityId = root.querySelector('#sitelinkAIActivity').value;
      var areaId = root.querySelector('#sitelinkAIArea').value;
      var button = root.querySelector('#sitelinkAISubmit');
      var resultRoot = root.querySelector('#sitelinkAIResult');
      if (!question) return;
      button.disabled = true;
      button.textContent = 'Reviewing project evidence…';
      resultRoot.innerHTML = '<div class="empty-state"><strong>Checking authorized project records</strong>Unreviewed reports are not used as approved evidence.</div>';
      var route = areaId ? '/api/ai/investigate' : '/api/ai/ask';
      var payload = { projectId: projectId, message: question, activityId: activityId };
      if (areaId) { payload.scope = 'area'; payload.areaId = areaId; }
      api.api(route, 'POST', payload).then(function (response) {
        resultRoot.innerHTML = renderResult(response);
      }).catch(function (error) {
        resultRoot.innerHTML = '<section class="panel ai-response ai-error"><strong>Could not answer from this scope</strong><p>' + escapeHtml(error.message || 'Check your access and try again.') + '</p><small>No project records were changed.</small></section>';
      }).finally(function () {
        button.disabled = false;
        button.textContent = 'Ask SiteLink';
      });
    });
    root.querySelectorAll('[data-ai-prompt]').forEach(function (button) {
      button.addEventListener('click', function () {
        root.querySelector('#sitelinkAIQuestion').value = button.getAttribute('data-ai-prompt');
        root.querySelector('#sitelinkAIQuestion').focus();
      });
    });
    root.querySelector('#sitelinkAIActivity').addEventListener('change', function () { pendingActivityId = this.value; });
  }

  api.registerView('sitelink-ai', {
    eyebrow: 'EXECUTION INTELLIGENCE · EVIDENCE-LED Q&A',
    title: 'Ask SiteLink',
    description: 'Investigate project execution with scoped schedule calculations and reviewed evidence.'
  }, render, bind);
  window.SiteLinkAIUI = {
    openForActivity: function (activityId) {
      pendingActivityId = String(activityId || '');
      api.navigate('sitelink-ai');
      window.setTimeout(function () {
        var question = document.getElementById('sitelinkAIQuestion');
        if (question) question.focus();
      }, 0);
    }
  };
})();
