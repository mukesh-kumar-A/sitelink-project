/* Evidence-led project home assembled from the existing deterministic APIs. */
(function () {
  'use strict';
  var S = window.SiteLinkControl;
  if (!S) return;

  var cached = null;
  var cachedProject = '';
  var cachedAt = 0;
  var pending = null;
  var requestVersion = 0;
  var loadError = '';
  var title = ['PROJECT HOME', 'Project overview', 'A live view of project progress, field evidence, decisions, and what needs attention.'];

  function esc(value) { return S.escape(value == null ? '' : String(value)); }
  function projectId() { return S.getProjectId() || ''; }
  function state() { return S.getState() || {}; }
  function countLabel(count, singular, plural) {
    var many = plural || (singular.slice(-1) === 'y' ? singular.slice(0, -1) + 'ies' : singular + 's');
    return count + ' ' + (count === 1 ? singular : many);
  }
  function records(ids) {
    return (ids || []).filter(Boolean).map(function (id) { return '<code>' + esc(id) + '</code>'; }).join(' ');
  }
  function goButton(label, view, cls) {
    return '<button type="button" class="' + (cls || 'panel-action') + '" data-home-nav="' + esc(view) + '">' + esc(label) + '</button>';
  }
  function load(force) {
    var pid = projectId();
    if (!pid) return Promise.resolve(null);
    if (!force && cached && cachedProject === pid && Date.now() - cachedAt < 8000) return Promise.resolve(cached);
    if (pending && pending.projectId === pid) return pending.promise;
    var version = ++requestVersion;
    loadError = '';
    var promise = Promise.all([
      S.api('/api/project-control?projectId=' + encodeURIComponent(pid), 'GET'),
      S.api('/api/execution-intelligence?projectId=' + encodeURIComponent(pid), 'GET')
    ]).then(function (result) {
      if (version !== requestVersion || projectId() !== pid) return null;
      cached = { control: result[0], execution: result[1] };
      cachedProject = pid;
      cachedAt = Date.now();
      return cached;
    }).catch(function (error) {
      if (version === requestVersion && projectId() === pid) loadError = error && error.message || 'Project summary could not be loaded.';
      return null;
    }).finally(function () {
      if (pending && pending.version === version) pending = null;
      if (version === requestVersion && ['overview', 'memory'].indexOf(S.getCurrentView()) >= 0) S.rerender();
    });
    pending = { projectId: pid, version: version, promise: promise };
    return promise;
  }
  function eventCount(execution, kind) {
    return (execution.thread || []).filter(function (item) { return item.kind === kind; }).length;
  }
  function renderLoading() {
    if (!pending && !loadError) load();
    if (loadError) return '<section class="panel home-load-error"><div><strong>Project overview is unavailable</strong><p>' + esc(loadError) + '</p></div><button class="button button-secondary" type="button" data-home-retry>Retry</button></section>';
    return '<section class="panel home-loading" role="status">Loading the current project summary…</section>';
  }
  function renderHealth(control) {
    var progress = control.progress || {};
    var counts = control.conditionCounts || {};
    var blocked = Number(counts.Blocked || 0);
    var delayed = Number(counts.Delayed || 0);
    var atRisk = Number(counts['At Risk'] || 0);
    var status = blocked ? 'Blocked work needs attention' : delayed ? 'Delayed work needs attention' : atRisk ? 'At-risk activities need review' : 'No delayed or blocked activities recorded';
    var tone = blocked || delayed ? 'warn' : atRisk ? 'caution' : 'good';
    var baseline = control.currentBaseline;
    var forecast = control.forecast || {};
    var forecastValue = forecast.estimatedCompletion ? esc(forecast.estimatedCompletion) : 'Not estimated';
    var forecastExplanation = forecast.estimatedCompletion
      ? esc(forecast.basisRecords || 0) + ' approved progress records · historical rate only'
      : 'Insufficient approved history for a completion estimate';
    var planned = progress.planned == null ? '—' : esc(progress.planned) + '%';
    var variance = Number(progress.variance || 0);
    var varianceLabel = (variance > 0 ? '+' : '') + esc(variance) + ' points vs planned';
    var progressValue = Math.max(0, Math.min(100, Number(progress.actual || 0)));
    return '<section class="home-health panel"><div class="panel-header"><div class="panel-heading"><h2>Project health</h2><p>' + esc(status) + '</p></div><span class="home-status ' + tone + '">' + esc(blocked + delayed + atRisk) + ' activities flagged</span></div>' +
      '<div class="home-health-body"><div class="home-progress-card"><div class="home-progress-number"><strong>' + esc(progress.actual == null ? '0' : progress.actual) + '%</strong><span>weighted actual progress</span></div><div class="home-progress-track" role="progressbar" aria-label="Weighted actual project progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="' + progressValue + '"><span style="width:' + progressValue + '%"></span></div><div class="home-health-meta"><span>Planned ' + planned + '</span><span>' + varianceLabel + '</span></div><small>Calculated from ' + countLabel(Number(progress.activityCount || 0), 'activity') + ' using ' + esc(progress.weighting || 'recorded schedule weights') + '.</small></div>' +
      '<div class="home-health-facts"><div><small>Approved baseline</small><strong>' + (baseline ? 'Version ' + esc(baseline.version) : 'No approved baseline') + '</strong><span>' + (baseline ? esc(baseline.approvedAt || 'Approval date not recorded') + ' · ' + countLabel(Number(baseline.activityCount || 0), 'activity') : 'Review an imported plan to establish the project baseline.') + '</span></div><div><small>Completion estimate</small><strong>' + forecastValue + '</strong><span>' + forecastExplanation + '</span></div></div></div></section>';
  }
  function renderAttention(control, execution) {
    var activities = control.activities || [];
    var conditions = (control.conditions || []).filter(function (item) { return ['Blocked', 'Delayed', 'At Risk'].indexOf(item.status) >= 0; });
    var priority = { Blocked: 0, Delayed: 1, 'At Risk': 2 };
    conditions.sort(function (a, b) { return (priority[a.status] - priority[b.status]) || String(a.activityId).localeCompare(String(b.activityId)); });
    var rows = conditions.slice(0, 4).map(function (condition) {
      var activity = activities.find(function (item) { return item.id === condition.activityId; }) || {};
      var reason = (condition.reasons || []).slice(0, 2).join(' ');
      return '<article class="home-attention-item"><div><small>' + esc(condition.status) + ' · ' + esc(activity.id || condition.activityId) + '</small><strong>' + esc(activity.name || 'Activity details unavailable') + '</strong><p>' + esc(reason || 'Review current progress and source records.') + '</p><span>' + esc(condition.actualProgress == null ? 'Actual progress not recorded' : condition.actualProgress + '% actual') + (condition.plannedProgress == null ? '' : ' · ' + esc(condition.plannedProgress) + '% planned') + '</span></div><div class="home-attention-actions"><button type="button" class="table-link" data-activity="' + esc(activity.id || condition.activityId) + '">Details</button><button type="button" class="table-link" data-home-ask="' + esc(activity.id || condition.activityId) + '">Ask AI</button></div></article>';
    }).join('');
    var causes = execution.analysis && execution.analysis.causes || [];
    var openRisks = (control.risks || []).filter(function (risk) { return ['Open', 'Monitoring'].indexOf(risk.status) >= 0; }).length;
    return '<section class="panel"><div class="panel-header"><div class="panel-heading"><h2>Needs attention</h2><p>Current schedule conditions and open project records</p></div>' + goButton('Open attention', 'risks') + '</div><div class="panel-body"><div class="home-attention-list">' +
      (rows || '<div class="empty-state"><strong>No delayed, blocked, or at-risk activity is recorded.</strong>Schedule conditions are calculated from current project records.</div>') +
      '</div><div class="home-attention-summary"><span>' + countLabel(conditions.length, 'flagged activity') + '</span><span>' + countLabel(causes.length, 'linked constraint') + '</span><span>' + countLabel(openRisks, 'open risk') + '</span></div></div></section>';
  }
  function renderDecisions(appState, execution) {
    var reports = (appState.reports || []).filter(function (item) { return !item.archived && ['pending', 'unmatched'].indexOf(String(item.status || '').toLowerCase()) >= 0; });
    var proposals = (execution.recommendations || []).filter(function (item) { return item.status === 'Proposed'; });
    var scenarios = (execution.scenarios || []).filter(function (item) { return item.status === 'Proposed'; });
    var rows = proposals.slice(0, 2).map(function (item) { var p = item.payload || {}; return '<div class="home-decision-row"><span class="home-decision-mark">↗</span><div><strong>' + esc(p.title || 'Planner recommendation') + '</strong><small>' + esc(p.activityId || 'Project-level') + ' · planner review required</small></div></div>'; }).join('');
    rows += scenarios.slice(0, 2).map(function (item) { var p = item.payload || {}; return '<div class="home-decision-row"><span class="home-decision-mark">◇</span><div><strong>' + esc(p.strategyLabel || 'Recovery scenario') + '</strong><small>' + esc(p.activityId || 'Activity not recorded') + ' · scenario awaits a planner decision</small></div></div>'; }).join('');
    return '<section class="panel"><div class="panel-header"><div class="panel-heading"><h2>Decisions pending</h2><p>Proposals remain inactive until a planner reviews them.</p></div>' + goButton('Review decisions', 'execution') + '</div><div class="panel-body"><div class="home-decision-counts"><div><strong>' + proposals.length + '</strong><span>recommendations</span></div><div><strong>' + scenarios.length + '</strong><span>recovery scenarios</span></div><div><strong>' + reports.length + '</strong><span>field reports</span></div></div><div class="home-decision-list">' + (rows || '<div class="empty-state">No planner decisions are waiting.</div>') + '</div>' + (reports.length ? '<button type="button" class="panel-action" data-home-nav="inbox">Open ' + countLabel(reports.length, 'report') + ' for review ↗</button>' : '') + '</div></section>';
  }
  function renderInsights(execution) {
    var analysis = execution.analysis || {};
    var causes = analysis.causes || [];
    var impacts = analysis.impacts || [];
    var rows = causes.slice(0, 3).map(function (cause) {
      var linked = impacts.filter(function (item) { return item.causeId === cause.id; });
      var sourceIds = cause.sourceRecords || [];
      var evidenceIds = cause.evidenceIds || [];
      var certainty = cause.causeCertainty || 'Unknown';
      var evidence = records(sourceIds.concat(evidenceIds));
      return '<article class="home-insight-item"><div class="home-insight-head"><small>' + esc(cause.activityId || 'Project') + ' · ' + esc(cause.severity || 'Unclassified') + '</small><span>' + esc(certainty === 'Confirmed' ? 'Recorded as confirmed' : certainty === 'Inferred' ? 'Heuristic inference' : 'Cause unknown') + '</span></div><strong>' + esc(cause.title || cause.category || 'Recorded constraint') + '</strong><p>' + esc(cause.sourceText || 'No source description is available.') + '</p><small>' + (linked.length ? countLabel(linked.length, 'downstream activity') + ' affected' : 'No downstream activity linked') + ' · Source records ' + (evidence || 'not recorded') + '</small><button type="button" class="table-link" data-home-ask="' + esc(cause.activityId || '') + '">Ask about this record</button></article>';
    }).join('');
    var estimateCount = (analysis.activityForecasts || []).filter(function (item) { return !!item.estimatedFinish; }).length;
    return '<section class="panel"><div class="panel-header"><div class="panel-heading"><h2>Evidence-backed insights</h2><p>Current cause classifications and impacts from reviewed records; heuristic labels remain uncalibrated.</p></div>' + goButton('Open analysis', 'execution') + '</div><div class="panel-body"><div class="home-insight-list">' + (rows || '<div class="empty-state"><strong>No linked cause is currently recorded.</strong>Submitted reports that are awaiting review do not drive cause or forecast analysis.</div>') + '</div><div class="home-insight-foot"><span>' + countLabel(causes.length, 'linked cause') + '</span><span>' + countLabel(impacts.length, 'downstream impact') + '</span><span>' + countLabel(estimateCount, 'activity estimate') + ' with a stored finish date</span><small>Forecast source count: ' + esc((analysis.recordCounts || {}).forecastSourceRecords || 0) + ' records</small></div></div></section>';
  }
  function renderThread(control, execution, appState) {
    var analysis = execution.analysis || {};
    var approved = execution.approvedUpdates || [];
    var baselineCount = (control.baselines || []).length;
    var evidenceCount = eventCount(execution, 'evidence') + approved.reduce(function (sum, item) { return sum + (Array.isArray(item.attachments) ? item.attachments.length : 0); }, 0);
    var fingerprintCount = ((appState.intelligence || {}).fingerprints || []).length;
    var memoryItemCount = fingerprintCount + (execution.outcomes || []).length;
    var cpmReady = ((analysis.graph || {}).cpm || {}).status === 'calculated';
    var forecastCount = (analysis.activityForecasts || []).filter(function (item) { return !!item.estimatedFinish; }).length;
    var stages = [
      { name: 'Plan & baseline', count: baselineCount, detail: baselineCount ? 'Approved baseline stored' : 'No approved baseline', view: 'plans' },
      { name: 'Field execution', count: approved.length, detail: 'Planner-approved updates', view: 'today' },
      { name: 'Evidence', count: evidenceCount, detail: 'Stored evidence references', view: 'execution' },
      { name: 'Cause & impact', count: (analysis.causes || []).length, detail: countLabel((analysis.impacts || []).length, 'downstream impact'), view: 'execution' },
      { name: 'Forecast', count: forecastCount, detail: cpmReady ? 'History estimates + CPM calculated' : 'History estimates; CPM inputs incomplete', view: 'execution' },
      { name: 'Recovery review', count: (execution.scenarios || []).length + (execution.recommendations || []).length, detail: 'Saved proposals and decisions', view: 'execution' },
      { name: 'Action & outcome', count: (execution.actions || []).length + (execution.outcomes || []).length, detail: 'Recorded actions and outcomes', view: 'execution' },
      { name: 'Project memory', count: memoryItemCount, detail: 'Completed history and measured outcomes', view: 'memory' }
    ];
    var stagesHtml = stages.map(function (stage, index) {
      return '<button type="button" class="home-thread-stage ' + (stage.count ? 'has-records' : '') + '" data-home-nav="' + esc(stage.view) + '"><span class="home-thread-index">' + (index + 1) + '</span><strong>' + esc(stage.name) + '</strong><b>' + esc(stage.count) + '</b><small>' + esc(stage.detail) + '</small></button>';
    }).join('');
    var recent = (execution.thread || []).slice(0, 5).map(function (event) {
      var source = (event.sourceRecords || []).length ? 'Source ' + (event.sourceRecords || []).join(', ') : 'Source not recorded';
      return '<article class="home-thread-event"><small>' + esc(String(event.kind || 'record').replace(/_/g, ' ')) + ' · ' + esc(event.date || '') + '</small><strong>' + esc(event.title || 'Project event') + '</strong><span>' + esc(event.activityId || 'Project record') + ' · ' + esc(source) + '</span></article>';
    }).join('');
    return '<section class="panel home-digital-thread"><div class="panel-header"><div class="panel-heading"><h2>Execution digital thread</h2><p>Stages reflect records stored for this project. Select a stage to open its working screen.</p></div>' + goButton('Open full thread', 'execution') + '</div><div class="panel-body"><div class="home-thread-stages">' + stagesHtml + '</div><div class="home-thread-recent"><div><h3>Recent linked events</h3><p>Source IDs and dates come from saved project records.</p></div>' + (recent || '<div class="empty-state">The thread starts when a plan baseline, approved update, risk, or evidence record is saved.</div>') + '</div></div></section>';
  }
  function renderToday(control) {
    var date = (control.today || {}).date || '';
    var rows = (control.activities || []).filter(function (activity) {
      return activity.plannedStart && activity.plannedFinish && activity.plannedStart <= date && activity.plannedFinish >= date && Number(activity.progress || 0) < 100;
    }).slice(0, 5).map(function (activity) {
      return '<article class="home-work-row"><div><strong>' + esc(activity.id) + ' · ' + esc(activity.name) + '</strong><small>' + esc(activity.discipline || 'Discipline unknown') + ' · ' + esc(Number(activity.progress || 0)) + '% in the schedule</small></div><button type="button" class="table-link" data-activity="' + esc(activity.id) + '">Details</button><button type="button" class="table-link" data-home-ask="' + esc(activity.id) + '">Ask SiteLink</button></article>';
    }).join('');
    return '<section class="panel"><div class="panel-header"><div class="panel-heading"><h2>Today’s work</h2><p>' + esc(date || 'Project date not available') + ' · activities in their planned date window</p></div>' + goButton('Open work log', 'today') + '</div><div class="panel-body"><div class="home-work-list">' + (rows || '<div class="empty-state"><strong>No unfinished activity is scheduled for today.</strong>The list follows saved schedule dates and actual progress.</div>') + '</div></div></section>';
  }
  function render() {
    if (!cached || cachedProject !== projectId() || Date.now() - cachedAt > 8000) return renderLoading();
    var control = cached.control || {};
    var execution = cached.execution || {};
    var appState = state();
    var project = control.project || {};
    return '<div class="home-context"><div><small>CURRENT PROJECT</small><strong>' + esc(project.name || 'Selected project') + '</strong><span>' + esc(project.phase || project.status || 'Project workspace') + '</span></div><button type="button" class="button button-secondary" data-home-nav="sitelink-ai">Ask SiteLink</button></div>' +
      renderHealth(control) + '<div class="home-priority-grid">' + renderToday(control) + renderAttention(control, execution) + '</div>' +
      '<div class="home-secondary-grid">' + renderDecisions(appState, execution) + renderInsights(execution) + '</div>' +
      renderThread(control, execution, appState);
  }
  function bind(root) {
    root.addEventListener('click', function (event) {
      var retry = event.target.closest('[data-home-retry]');
      if (retry) { load(true); return; }
      var nav = event.target.closest('[data-home-nav]');
      if (nav) { S.navigate(nav.getAttribute('data-home-nav')); return; }
      var ask = event.target.closest('[data-home-ask]');
      if (ask) {
        var activityId = ask.getAttribute('data-home-ask') || '';
        if (activityId && window.SiteLinkAIUI) {
          window.SiteLinkAIUI.openForActivity(activityId);
          window.setTimeout(function () {
            var input = document.getElementById('sitelinkAIQuestion');
            if (input) input.value = 'Why does ' + activityId + ' need attention, and which project records support the explanation?';
          }, 20);
        } else S.navigate('sitelink-ai');
      }
    });
  }

  function renderMemoryPage() {
    if (!cached || cachedProject !== projectId() || Date.now() - cachedAt > 8000) return renderLoading();
    var legacy = typeof S.renderExistingMemory === 'function' ? S.renderExistingMemory() : '<div class="empty-state">Historical execution fingerprints are unavailable.</div>';
    var execution = cached.execution || {};
    var actions = execution.actions || [];
    var outcomes = (execution.outcomes || []).slice().sort(function (a, b) {
      return String((b.payload || {}).recordedAt || b.createdAt || '').localeCompare(String((a.payload || {}).recordedAt || a.createdAt || ''));
    });
    var rows = outcomes.map(function (record) {
      var p = record.payload || {};
      var action = actions.find(function (item) { return item.id === p.actionId; });
      var actionPayload = action && action.payload || {};
      var before = (p.forecastBefore || []).find(function (item) { return item.activityId === p.activityId; }) || {};
      var after = (p.forecastAfter || []).find(function (item) { return item.activityId === p.activityId; }) || {};
      var measure = p.actualFinishDate || (p.actualDaysSaved == null ? 'No numeric measure recorded' : p.actualDaysSaved + ' calendar day(s) recovered');
      var sourceIds = (p.sourceRecords || []).filter(Boolean);
      return '<article class="memory-outcome-card"><div class="memory-outcome-head"><div><small>RECORDED OUTCOME · ' + esc(p.recordedAt || record.createdAt || 'Date not recorded') + '</small><h3>' + esc(p.activityId || 'Project action') + ' · ' + esc(actionPayload.title || actionPayload.action || 'Approved execution action') + '</h3></div><span>' + esc(measure) + '</span></div><p><b>What happened:</b> ' + esc(p.result || 'Outcome details not recorded.') + '</p><p><b>Lesson for future decisions:</b> ' + esc(p.lesson || 'No lesson was recorded for this outcome.') + '</p><div class="memory-outcome-meta"><span>Recorded by ' + esc(p.recordedBy || 'Reviewer not recorded') + '</span><span>Reason: ' + esc(p.reason || 'Not recorded') + '</span><span>Action record ' + esc(p.actionId || 'Not linked') + '</span></div><div class="memory-outcome-forecast"><div><small>Estimate before outcome</small><strong>' + esc(before.estimatedFinish || 'No estimate') + '</strong></div><div><small>Estimate after outcome</small><strong>' + esc(after.estimatedFinish || 'Insufficient data') + '</strong></div></div><small class="memory-outcome-source">Source records: ' + (sourceIds.length ? records(sourceIds) : 'none linked') + ' · outcome ' + esc(record.id) + '</small></article>';
    }).join('');
    return legacy + '<section class="panel memory-outcome-panel"><div class="panel-header"><div class="panel-heading"><h2>Recovery outcome memory</h2><p>Measured results and lessons are retained as project history; they are not promises about future work.</p></div><span class="role-badge">' + countLabel(outcomes.length, 'recorded outcome') + '</span></div><div class="panel-body"><div class="memory-outcome-list">' + (rows || '<div class="empty-state"><strong>No recovery outcomes are recorded yet.</strong>After a planner-approved action, record what happened, a supported lesson, and a measured finish date or days recovered.</div>') + '</div></div></section>';
  }

  S.registerView('overview', { eyebrow: title[0], title: title[1], description: title[2] }, render, bind);
  S.registerView('memory', { eyebrow: 'SITELINK AI · PROJECT MEMORY', title: 'Execution memory', description: 'Review completed activity history and planner-recorded recovery outcomes for this project.' }, renderMemoryPage, bind);
})();
