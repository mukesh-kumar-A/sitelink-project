/* Execution intelligence surfaces for the existing SiteLink v4 application. */
(function () {
  'use strict';
  var S = window.SiteLinkControl;
  if (!S) return;
  var viewData = null, loadedAt = 0, loadedProject = '', loading = false;
  var heading = ['EXECUTION INTELLIGENCE', 'Execution intelligence', 'Trace approved field evidence through causes, dependency effects, forecasts, human decisions, and recorded outcomes.'];
  function esc(value) { return S.escape(value == null ? '' : String(value)); }
  function projectId() { return S.getProjectId() || ''; }
  function user() { return S.getUser() || {}; }
  function planner() { return !!(viewData && viewData.permissions && viewData.permissions.approve); }
  function card(title, body, extra) { return '<section class="ei-card"><header><h2>' + esc(title) + '</h2>' + (extra || '') + '</header>' + body + '</section>'; }
  function pill(text, cls) { return '<span class="ei-pill ' + (cls || '') + '">' + esc(text) + '</span>'; }
  function reasonInput(label) { return '<label class="ei-label">' + esc(label || 'Decision reason') + '<input name="reason" required minlength="8" maxlength="500" placeholder="Explain the decision or assumption"></label>'; }
  function post(path, value) { return S.api(path, 'POST', Object.assign({ projectId: projectId() }, value || {})); }
  function load(force) {
    if (!force && viewData && loadedProject === projectId() && Date.now() - loadedAt < 7000) return Promise.resolve(viewData);
    if (loading) return Promise.resolve(viewData);
    loading = true;
    return S.api('/api/execution-intelligence?projectId=' + encodeURIComponent(projectId()), 'GET')
      .then(function (result) { viewData = result; loadedAt = Date.now(); loadedProject = projectId(); return result; })
      .catch(function (error) { S.toast(error.message || 'Could not load execution intelligence.', 'error'); return viewData; })
      .finally(function () { loading = false; S.rerender(); });
  }
  function formatRecords(values) {
    return (values || []).map(function (id) {
      var known = viewData && ['scenarios', 'recommendations', 'actions', 'outcomes'].some(function (key) {
        return (viewData[key] || []).some(function (record) { return record.id === id; });
      });
      return known ? '<a class="ei-record-link" href="#execution-record-' + esc(id) + '">' + esc(id) + '</a>' : '<code>' + esc(id) + '</code>';
    }).join(' ');
  }
  function evidenceLinks(ids) {
    return (ids || []).map(function (id) { return '<a href="/api/documents/' + encodeURIComponent(id) + '/download?projectId=' + encodeURIComponent(projectId()) + '" target="_blank" rel="noopener">Evidence ' + esc(id) + '</a>'; }).join(' · ');
  }
  function renderGraph(analysis) {
    var graph = analysis.graph || {}, nodes = graph.nodes || [];
    if (!nodes.length) return '<p class="ei-empty">No schedule activities are available in this project yet. Upload a plan, review its extracted rows, and approve the baseline first.</p>';
    var cpm = graph.cpm || {}, cpmReady = cpm.status === 'calculated';
    var nodeHtml = nodes.map(function (n) {
      var relations = (graph.edges || []).filter(function (edge) { return edge.to === n.id; }).map(function (edge) { return edge.from + ' (' + edge.type + (edge.lagDays ? ', ' + edge.lagDays + 'd lag' : '') + ')'; });
      var cp = n.cpm || {};
      var timing = cpmReady
        ? 'CPM ES/EF ' + esc(cp.earlyStart || ('day ' + cp.earlyStartOffset)) + ' / ' + esc(cp.earlyFinish || ('day ' + cp.earlyFinishOffset)) + ' · LS/LF ' + esc(cp.lateStart || ('day ' + cp.lateStartOffset)) + ' / ' + esc(cp.lateFinish || ('day ' + cp.lateFinishOffset)) + ' · total/free float ' + esc(cp.totalFloatDays) + ' / ' + esc(cp.freeFloatDays) + ' days'
        : 'CPM timing unavailable; duration-path heuristic only where highlighted.';
      var critical = cpmReady ? !!n.onCpmCritical : !!n.onCriticalChain;
      return '<article class="ei-node ' + (critical ? 'critical' : '') + '"><small>' + esc(n.id) + (critical ? (cpmReady ? ' · CPM critical' : ' · duration-path heuristic') : '') + '</small><strong>' + esc(n.name) + '</strong><span>' + esc(n.area || 'Area not recorded') + ' › ' + esc(n.subArea || 'Sub-area not recorded') + '</span><span>Progress ' + esc(n.progress) + '% · Planned finish ' + esc(n.plannedFinish || 'Unknown') + ' · Duration ' + esc(cp.durationDays == null ? 'unknown' : cp.durationDays + ' calendar days') + '</span><span>' + timing + '</span><span>Predecessors: ' + esc(relations.join(', ') || 'None recorded') + '</span><span>Successors: ' + esc(n.successors.join(', ') || 'None recorded') + '</span></article>';
    }).join('');
    var issues = (graph.issues || []).map(function (item) { return '<p class="ei-warning">' + esc(item.type === 'cycle' ? 'Dependency cycle needs planner correction: ' + item.activityIds.join(', ') : item.activityId + ' references unknown predecessor ' + item.reference) + '</p>'; }).join('');
    var paths = cpmReady ? (cpm.criticalPaths || []).map(function (path) { return path.join(' → '); }).join(' · ') : '';
    var cpmSummary = cpmReady
      ? '<p class="ei-method">Calendar-day CPM calculated from recorded durations, predecessor types, lags, and planned date constraints. Project duration ' + esc(cpm.projectDurationDays) + ' days' + (cpm.projectFinishDate ? ' · finish ' + esc(cpm.projectFinishDate) : '') + '. Critical path: ' + esc(paths || 'No linked critical path') + '. Resource leveling is not included.</p>'
      : '<p class="ei-warning">CPM not calculated: ' + esc(cpm.basis || 'schedule inputs are incomplete') + (cpm.missingDurationActivityIds && cpm.missingDurationActivityIds.length ? ' Missing durations: ' + esc(cpm.missingDurationActivityIds.join(', ')) : '') + '. Highlighted nodes, if any, are only a duration-path heuristic.</p>';
    return cpmSummary + issues + '<div class="ei-graph">' + nodeHtml + '</div>';
  }
  function renderCauses(analysis) {
    var causes = analysis.causes || [], impacts = analysis.impacts || [];
    if (!causes.length) return '<p class="ei-empty">No current blocker or issue is linked to an activity. Approved update history is checked; submitted but unreviewed reports do not drive causes.</p>';
    return '<div class="ei-stack">' + causes.map(function (cause) {
      var related = impacts.filter(function (i) { return i.causeId === cause.id; });
      return '<article class="ei-cause"><div class="ei-cause-top"><div><small>' + esc(cause.recordType.replace('_', ' ')) + ' · ' + esc(cause.activityId) + '</small><h3>' + esc(cause.title) + '</h3></div>' + pill(cause.category + ' · ' + cause.severity, cause.severity === 'High' || cause.severity === 'Critical' ? 'warn' : '') + ' ' + pill('Cause certainty: ' + (cause.causeCertainty || 'Unknown'), cause.causeCertainty === 'Confirmed' ? 'good' : 'warn') + '</div><p>' + esc(cause.sourceText || '') + '</p><small>' + esc(cause.area || 'Area not recorded') + ' · ' + esc(cause.subArea || 'Sub-area not recorded') + ' · Classification: ' + esc(cause.classificationBasis) + ' · Source: ' + formatRecords(cause.sourceRecords) + '</small>' + (cause.evidenceIds.length ? '<div class="ei-evidence">' + evidenceLinks(cause.evidenceIds) + '</div>' : '') + (related.length ? '<div class="ei-impact-list"><strong>Downstream schedule impact (' + related.length + ' activities)</strong>' + related.map(function (impact) { return '<div><b>' + esc(impact.activityId) + ' · ' + esc(impact.activityName) + '</b><span>' + esc(impact.area) + ' · ' + esc(impact.subArea || 'Sub-area not recorded') + ' · ' + esc(impact.dependencyType) + ' · ' + esc(impact.impactStatus) + (impact.estimatedDelayDays == null ? '' : ' · ' + esc(impact.estimatedDelayDays) + ' days · ' + Math.round(impact.confidence * 100) + '% heuristic') + '</span><small>Path ' + esc((impact.dependencyPath || []).join(' → ')) + ' · Milestones: ' + esc((impact.affectedMilestones || []).join(', ') || 'No milestone link') + ' · Source ' + formatRecords(impact.sourceRecords) + ' · ' + esc(impact.estimateBasis) + '</small></div>'; }).join('') + '</div>' : '<div class="ei-impact-list"><strong>No downstream activity is linked from this cause in the current schedule.</strong></div>') + '</article>';
    }).join('') + '</div>';
  }
  function renderAreaInsights(analysis) {
    var groups = analysis.areaInsights || [];
    if (!groups.length) return '<p class="ei-empty">No schedule activities have been assigned to an area yet. Area and sub-area values remain unknown until a planner records them.</p>';
    return '<div class="ei-area-list">' + groups.map(function (g) {
      return '<article class="ei-record"><div class="ei-record-head"><div><small>' + esc(g.area) + (g.areaId ? ' · ' + esc(g.areaId) : '') + '</small><h3>' + esc(g.subArea) + (g.subAreaId ? ' · ' + esc(g.subAreaId) : '') + '</h3></div><span class="ei-method">' + esc(g.activityCount) + ' activities</span></div><div class="ei-data-grid"><div><small>Approved updates</small><strong>' + esc(g.approvedUpdateCount) + '</strong></div><div><small>Linked causes</small><strong>' + esc(g.linkedCauseCount) + '</strong></div><div><small>Impacted activities</small><strong>' + esc(g.affectedActivityCount) + '</strong></div></div><small>Latest approved update: ' + esc(g.latestApprovedUpdate || 'None recorded') + ' · Source records: ' + formatRecords(g.sourceRecords) + '</small><p class="ei-method">Activity IDs: ' + esc((g.activityIds || []).join(', ')) + '</p></article>';
    }).join('') + '</div>';
  }
  function renderForecast(analysis) {
    var forecasts = analysis.activityForecasts || [], milestones = analysis.milestoneForecasts || [];
    var rows = forecasts.map(function (f) { return '<tr><td><strong>' + esc(f.activityId) + '</strong><small>' + esc(f.activityName) + '</small></td><td>' + esc(f.status) + '<small>' + esc(f.explanation || '') + '</small></td><td>' + esc(f.estimatedFinish || 'Not enough approved history') + '</td><td>' + esc(f.plannedFinish || '—') + (f.varianceDays == null ? '' : '<small>' + (f.varianceDays > 0 ? '+' : '') + esc(f.varianceDays) + ' calendar days vs baseline</small>') + '</td><td>' + esc(f.basisRecords || 0) + '</td><td>' + esc(f.confidence || '—') + '</td></tr>'; }).join('');
    var ms = milestones.length ? '<div class="ei-milestones">' + milestones.map(function (m) { return '<div><strong>' + esc(m.name) + '</strong><span>Baseline ' + esc(m.plannedDate || 'Unknown') + ' · CPM-linked date ' + esc(m.cpmEstimatedDate || 'Unavailable') + ' · historical estimate ' + esc(m.estimatedDate || 'Insufficient approved activity history') + '</span><small>' + (m.cpmVarianceDays == null ? 'No CPM variance available' : esc(m.cpmVarianceDays) + ' calendar days vs milestone baseline') + ' · ' + (m.varianceDays == null ? 'No history-based variance available' : esc(m.varianceDays) + ' calendar days history variance') + ' · ' + esc(m.basisRecords) + ' history records · linked: ' + esc(m.linkedActivities.join(', ') || 'none') + ' · ' + esc(m.cpmBasis || '') + '</small></div>'; }).join('') + '</div>' : '<p class="ei-empty">No project milestones recorded.</p>';
    return '<p class="ei-method">Approved reports and recorded outcomes support historical forecasts. Separately, CPM-linked milestone dates come from schedule durations and relationships; they are schedule calculations, not execution predictions. Each estimate names its basis and records.</p><div class="ei-table-wrap"><table class="ei-table"><thead><tr><th>Activity</th><th>Basis and explanation</th><th>Estimate</th><th>Baseline / variance</th><th>Records</th><th>Confidence</th></tr></thead><tbody>' + (rows || '<tr><td colspan="6">No activities found.</td></tr>') + '</tbody></table></div><h3 class="ei-subhead">Milestones</h3>' + ms;
  }
  function renderScenario(scenario) {
    var p = scenario.payload || {};
    var affected = (p.affectedActivities || []).map(function (a) { return a.id + ' · ' + a.name; }).join(', ');
    var m = p.modelInputs || {};
    var model = '<p><b>Work remaining:</b> ' + esc(m.remainingProgressPct == null ? 'Unknown' : m.remainingProgressPct + '%') + ' · <b>Observed rate:</b> ' + esc(m.observedDailyProgressPct == null ? 'Unavailable' : m.observedDailyProgressPct + ' percentage points/day') + ' · <b>Recorded crew:</b> ' + esc(m.currentManpower == null ? 'Unknown' : m.currentManpower + ' people') + (m.currentManpowerSource ? ' · Source ' + esc(m.currentManpowerSource) : '') + ' · <b>Equipment in latest report:</b> ' + esc(m.currentEquipmentUsed || 'Unknown') + ' · <b>Material:</b> ' + esc(m.materialStatus || 'Unknown') + ' (' + esc(m.materialStatusSource || 'not recorded') + ')</p>';
    var dependency = p.dependencyChanges && p.dependencyChanges.length ? p.dependencyChanges.map(function (change) { return esc(change.proposal + ': ' + change.from + ' → ' + change.to + '. ' + change.cycleCheck + ' ' + change.limitation); }).join(' ') : 'No dependency change proposed; saved schedule links remain unchanged.';
    return '<article class="ei-record" id="execution-record-' + esc(scenario.id) + '"><div class="ei-record-head"><div><small>Recovery scenario · ' + esc(scenario.createdAt) + '</small><h3>' + esc(p.strategyLabel || 'Scenario') + ' · ' + esc(p.activityId || '') + '</h3></div>' + pill(scenario.status, scenario.status === 'Approved' ? 'good' : scenario.status === 'Rejected' ? 'muted' : 'warn') + '</div><p>' + esc(p.activityName || '') + '</p><div class="ei-data-grid"><div><small>Reference finish</small><strong>' + esc(p.referenceFinish || 'Not estimated') + '</strong></div><div><small>Scenario finish</small><strong>' + esc(p.scenarioFinish || 'Insufficient project data') + '</strong></div><div><small>Baseline variance</small><strong>' + esc(p.baselineVarianceDays == null ? 'Not available' : p.baselineVarianceDays + ' days') + '</strong></div></div>' + model + '<p><b>Resource change:</b> ' + esc(p.resourceChange || 'None specified') + '</p><p><b>Assumptions:</b> ' + esc(p.assumptions || '') + '</p><p><b>Affected activities:</b> ' + esc(affected || 'Not calculated') + '</p><p><b>Dependency review:</b> ' + dependency + '</p><p><b>Risk:</b> ' + esc(p.riskAssessment || 'Not quantified') + '</p><p class="ei-method">' + esc(p.basis || '') + ' ' + esc(p.confidence || '') + '</p><small>Source: ' + formatRecords(p.sourceRecords) + '</small>' + (scenario.status === 'Proposed' && planner() && p.scenarioFinish ? '<div class="ei-decision"><input class="ei-reason" minlength="8" placeholder="Reason for approve / reject" aria-label="Decision reason"><button type="button" class="button button-primary" data-action="scenario-approve" data-id="' + esc(scenario.id) + '">Approve estimate</button><button type="button" class="button button-secondary" data-action="scenario-reject" data-id="' + esc(scenario.id) + '">Reject</button></div>' : p.decisionReason ? '<p class="ei-method">' + esc(p.decision) + ' by ' + esc(p.decidedBy) + ' · ' + esc(p.decisionReason) + '</p>' : '') + '</article>';
  }
  function renderRecommendation(record) {
    var p = record.payload || {};
    return '<article class="ei-record" id="execution-record-' + esc(record.id) + '"><div class="ei-record-head"><div><small>Next-day planner · ' + esc(p.category || 'Review') + ' · ' + esc(p.priority || '—') + ' priority</small><h3>' + esc(p.title || '') + '</h3></div>' + pill(record.status, record.status === 'Approved' ? 'good' : record.status === 'Rejected' ? 'muted' : 'warn') + '</div><p><b>Reason:</b> ' + esc(p.reason || '') + '</p><p><b>Linked activity:</b> ' + esc(p.activityId || 'Project level') + ' · ' + esc(p.area || 'Area not recorded') + ' › ' + esc(p.subArea || 'Sub-area not recorded') + '</p><p><b>Priority basis:</b> ' + esc(p.priorityReason || 'Planner review required') + '</p><p><b>Proposed action:</b> ' + esc(p.action || '') + '</p><p><b>Expected impact:</b> ' + esc(p.expectedImpact || 'No numerical impact estimate available.') + '</p>' + (p.affectedMilestones && p.affectedMilestones.length ? '<p><b>Linked milestones:</b> ' + esc(p.affectedMilestones.join(', ')) + '</p>' : '') + '<p class="ei-method">' + esc(p.confidence || '') + ' · Source ' + formatRecords(p.sourceRecords) + '</p>' + (p.evidenceIds && p.evidenceIds.length ? '<div class="ei-evidence">' + evidenceLinks(p.evidenceIds) + '</div>' : '') + (record.status === 'Proposed' && planner() ? '<label class="ei-label">Edit proposed action<input class="ei-edit-action" value="' + esc(p.action || '') + '"></label><div class="ei-decision"><input class="ei-reason" minlength="8" placeholder="Reason for decision" aria-label="Decision reason"><button type="button" class="button button-primary" data-action="recommend-approve" data-id="' + esc(record.id) + '">Approve action</button><button type="button" class="button button-secondary" data-action="recommend-edit" data-id="' + esc(record.id) + '">Save edit</button><button type="button" class="button button-secondary" data-action="recommend-reject" data-id="' + esc(record.id) + '">Reject</button></div>' : p.decisionReason ? '<p class="ei-method">Decision by ' + esc(p.decidedBy) + ': ' + esc(p.decisionReason) + '</p>' : '') + '</article>';
  }
  function renderAction(record, updates) {
    var p = record.payload || {};
    var outcome = (viewData.outcomes || []).find(function (o) { return o.id === p.outcomeId; });
    var outcomePayload = outcome && outcome.payload || {};
    var pendingForm = '<label class="ei-label">Observed outcome<textarea class="ei-outcome-result" minlength="8" rows="2" placeholder="What happened after the approved action?"></textarea></label><label class="ei-label">Lesson for future decisions<textarea class="ei-outcome-lesson" minlength="8" maxlength="1000" rows="2" placeholder="What does this result support learning?" required></textarea></label><div class="ei-fields"><label class="ei-label">Observed finish date<input class="ei-outcome-date" type="date"></label><label class="ei-label">Measured days recovered<input class="ei-outcome-days" type="number" min="0" max="365" step="1"></label><label class="ei-label">Link approved field update<select class="ei-outcome-update"><option value="">No linked update</option>' + (updates || []).map(function (u) { return '<option value="' + esc(u.id) + '">' + esc(u.workDate + ' · ' + u.activityId + ' · ' + (u.progress == null ? '—' : u.progress + '%')) + '</option>'; }).join('') + '</select></label></div><small class="ei-method">Enter an observed finish date or measured recovery days to update the estimate. Record only lessons supported by the observed result. This record does not edit schedule actuals.</small><div class="ei-decision"><input class="ei-reason" minlength="8" placeholder="Outcome record reason" aria-label="Outcome record reason"><button type="button" class="button button-primary" data-action="action-outcome" data-id="' + esc(record.id) + '">Record outcome</button></div>';
    var saved = p.outcomeId ? '<div class="ei-result" id="execution-record-' + esc(p.outcomeId) + '"><strong>Outcome recorded</strong><p>' + esc(outcomePayload.result || '') + '</p>' + (outcomePayload.lesson ? '<p><b>Lesson learned:</b> ' + esc(outcomePayload.lesson) + '</p>' : '<p><b>Lesson learned:</b> Not recorded for this outcome.</p>') + '<div class="ei-data-grid"><div><small>Forecast before</small><strong>' + esc((outcomePayload.forecastBefore || []).find(function (f) { return f.activityId === p.activityId; })?.estimatedFinish || 'No estimate') + '</strong></div><div><small>Forecast after recorded outcome</small><strong>' + esc((outcomePayload.forecastAfter || []).find(function (f) { return f.activityId === p.activityId; })?.estimatedFinish || 'Insufficient data') + '</strong></div><div><small>Recorded measure</small><strong>' + esc(outcomePayload.actualFinishDate || (outcomePayload.actualDaysSaved == null ? '—' : outcomePayload.actualDaysSaved + ' days recovered')) + '</strong></div></div><small>Source ' + formatRecords(outcomePayload.sourceRecords) + ' · forecast updated from a planner-recorded result; schedule actuals remain approval-gated.</small></div>' : '';
    return '<article class="ei-record" id="execution-record-' + esc(record.id) + '"><div class="ei-record-head"><div><small>' + esc(p.activityId || '') + ' · ' + esc(record.createdAt) + '</small><h3>' + esc(p.title || 'Execution action') + '</h3></div>' + pill(record.status, record.status === 'Completed' ? 'good' : 'warn') + '</div><p>' + esc(p.action || '') + '</p><p class="ei-method">Approved by ' + esc(p.decidedBy || p.approvedBy || user().name) + ' · ' + esc(p.reason || '') + '</p>' + (record.status === 'Approved' && planner() ? pendingForm : saved) + '</article>';
  }
  function render() {
    if (!viewData || Date.now() - loadedAt > 7000) { load(); return '<div class="ei-loading">Loading project execution history…</div>'; }
    var d = viewData, a = d.analysis || {}, counts = a.recordCounts || {}, acts = d.activities || [];
    var metrics = '<div class="ei-metrics"><div><small>Approved updates</small><strong>' + esc(counts.approvedUpdates || 0) + '</strong></div><div><small>Open causes</small><strong>' + esc(counts.causes || 0) + '</strong></div><div><small>Downstream activities</small><strong>' + esc(counts.affectedActivities || 0) + '</strong></div><div><small>Forecast source records</small><strong>' + esc(counts.forecastSourceRecords || 0) + '</strong></div></div>';
    var seed = !d.demoSeeded && user().role === 'Admin' ? '<section class="ei-demo"><div><strong>Judge walkthrough data</strong><p>Load a clearly marked synthetic material-delay story with linked evidence, a predecessor chain, a milestone, and reviewed progress.</p></div><button type="button" class="button button-primary" data-action="seed">Load synthetic story</button></section>' : '';
    var graph = '<details class="ei-advanced"><summary>Schedule structure <span>CPM, dependencies, and critical path</span></summary>' + card('Live dependency graph', renderGraph(a)) + '</details>';
    var areas = card('Area and sub-area execution', renderAreaInsights(a));
    var causes = card('Cause and downstream impact', renderCauses(a));
    var forecasts = card('Forecast and milestone exposure', renderForecast(a));
    var approved = d.permissions && d.permissions.approve && planner();
    var scenarioForm = approved ? '<form class="ei-form" data-form="scenario"><h3>Simulate a recovery option</h3><p class="ei-method">Where available, the estimate uses approved progress rate, work remaining, crew size, and saved dependency links. Missing inputs stay unknown; all productivity changes are labeled assumptions.</p><div class="ei-fields"><label class="ei-label">Affected activity<select name="activityId" required><option value="">Choose an activity</option>' + acts.map(function (x) { return '<option value="' + esc(x.id) + '">' + esc(x.id + ' · ' + x.name) + '</option>'; }).join('') + '</select></label><label class="ei-label">Recovery approach<select name="strategy" required><option value="material">Expedite or substitute material</option><option value="manpower">Add or reassign a crew</option><option value="equipment">Add or substitute equipment</option><option value="resequence">Resequence dependent work</option><option value="parallel">Propose parallel execution</option><option value="other">Other planner-defined recovery</option></select></label><label class="ei-label">Fallback days recovered<input name="daysSaved" type="number" min="0" max="30" value="1" required></label><label class="ei-label">Resource change being assumed<input name="resourceChange" minlength="3" maxlength="300" placeholder="Example: add 2 fitters for one shift"></label><label class="ei-label">Current crew count if not recorded<input name="currentManpower" type="number" min="0" max="10000" placeholder="Uses latest approved field update first"></label><label class="ei-label">Additional crew<input name="additionalManpower" type="number" min="0" max="10000" placeholder="Optional people added"></label><label class="ei-label">Assumed productivity change (%)<input name="assumedProductivityChangePct" type="number" min="0" max="200" step="0.1" placeholder="Optional explicit assumption"></label><label class="ei-label">Additional equipment<input name="additionalEquipment" maxlength="300" placeholder="Unknown unless entered"></label><label class="ei-label">Material availability<select name="materialStatus"><option>Unknown</option><option>Available</option><option>Limited</option><option>Blocked</option></select></label><label class="ei-label">Direct successor to overlap<select name="parallelActivityId"><option value="">Choose only for parallel scenario</option>' + acts.map(function (x) { return '<option value="' + esc(x.id) + '">' + esc(x.id + ' · ' + x.name) + '</option>'; }).join('') + '</select></label></div><label class="ei-label">Assumptions<textarea name="assumptions" minlength="8" maxlength="1200" rows="2" required placeholder="State what resource, productivity, material, or sequencing change is assumed."></textarea></label>' + reasonInput('Reason for running this scenario') + '<button class="button button-primary">Run estimate</button></form>' : '';
    var scenarios = (d.scenarios || []).map(renderScenario).join('') || '<p class="ei-empty">No recovery scenarios saved. A planner can test a clearly stated assumption above.</p>';
    var actions = (d.actions || []).map(function (r) { return renderAction(r, d.approvedUpdates); }).join('') || '<p class="ei-empty">No approved execution actions yet.</p>';
    var recForm = approved ? '<form class="ei-form ei-inline-form" data-form="generate"><div><h3>Prepare next-day recommendations</h3><p>Uses linked causes from approved updates and open project risks. Review each proposal before it becomes an action.</p></div>' + reasonInput('Reason for generating proposals') + '<button class="button button-primary">Generate plan</button></form>' : '';
    var recs = (d.recommendations || []).map(renderRecommendation).join('') || '<p class="ei-empty">No recommendations saved. Generate a planner review from supported project causes.</p>';
    var thread = (d.thread || []).slice(0, 40).map(function (e) { var activity = (d.activities || []).find(function (item) { return item.id === e.activityId; }) || {}; var location = activity.area || activity.location || (e.areaId ? 'Area ' + e.areaId : 'Area not recorded'); var subArea = activity.subArea || (e.subAreaId ? 'Sub-area ' + e.subAreaId : 'Sub-area not recorded'); return '<div class="ei-thread-row"><span class="ei-thread-dot"></span><div><small>' + esc(e.kind.replace('_', ' ')) + ' · ' + esc(e.date || '') + '</small><strong>' + esc(e.title) + '</strong><span>' + esc(e.activityId || 'Project record') + ' · ' + esc(location) + ' › ' + esc(subArea) + ' · ' + formatRecords(e.sourceRecords) + '</span>' + (e.evidenceIds && e.evidenceIds.length ? '<span>' + evidenceLinks(e.evidenceIds) + '</span>' : '') + '</div></div>'; }).join('') || '<p class="ei-empty">Execution thread starts with approved work updates and recorded risks.</p>';
    return seed + metrics + '<div class="ei-grid"><div class="ei-main">' + graph + areas + causes + forecasts + card('Recovery simulator', scenarioForm + '<div class="ei-stack">' + scenarios + '</div>') + card('Tomorrow’s execution plan', recForm + '<div class="ei-stack">' + recs + '</div>') + card('Approved actions and actual outcomes', '<div class="ei-stack">' + actions + '</div>') + '</div><aside>' + card('Project execution thread', '<p class="ei-method">Events link the activity, source record, supporting evidence, and human decision. Synthetic sample records are labeled.</p><div class="ei-thread">' + thread + '</div>') + card('Method and boundaries', '<p>' + esc(a.method || '') + '</p><ul class="ei-boundaries"><li>Dependency links come from saved schedule predecessors.</li><li>Cause classification is a transparent terminology heuristic.</li><li>Forecasts require approved history or a recorded planner-observed outcome.</li><li>Productivity estimates use explicit assumptions and available approved records; they are not a validated resource optimizer.</li><li>Parallel work is a proposal only; saved schedule links stay unchanged and safety constraints need planner review.</li><li>Only the existing planner approval of a field update changes schedule actuals.</li></ul>') + '</aside></div>';
  }
  function values(form) { var result = {}; new FormData(form).forEach(function (v, k) { result[k] = v; }); return result; }
  function refresh(message) { if (message) S.toast(message); return load(true).then(function () { return S.rerender(); }); }
  function action(name, id, root) {
    if (name === 'seed') {
      return post('/api/demo/seed-execution-storyline', { reason: 'Loaded synthetic execution history for the SiteLink judge walkthrough.' })
        .then(function () { return refresh('Synthetic execution storyline loaded. All sample evidence is labeled.'); });
    }
    var cardRoot = root.querySelector('[data-id="' + CSS.escape(id) + '"]').closest('.ei-record');
    var reasonEl = cardRoot.querySelector('.ei-reason'), reason = reasonEl ? reasonEl.value.trim() : '';
    if (reason.length < 8) throw new Error('Enter an audited reason of at least 8 characters.');
    if (name === 'scenario-approve' || name === 'scenario-reject') {
      return post('/api/recovery-scenarios/' + encodeURIComponent(id) + '/decision', { decision: name === 'scenario-approve' ? 'Approved' : 'Rejected', reason: reason })
        .then(function () { return refresh(name === 'scenario-approve' ? 'Scenario approved as an execution action. No schedule actuals changed.' : 'Scenario rejected and retained in history.'); });
    }
    if (name.indexOf('recommend-') === 0) {
      var decision = name.substring('recommend-'.length), body = { decision: decision, reason: reason };
      if (decision === 'edit') body.action = cardRoot.querySelector('.ei-edit-action').value.trim();
      return post('/api/recommendations/' + encodeURIComponent(id) + '/decision', body)
        .then(function () { return refresh(decision === 'approve' ? 'Planner recommendation approved as an action.' : decision === 'edit' ? 'Recommendation edit saved for another review.' : 'Recommendation rejected and retained in history.'); });
    }
    if (name === 'action-outcome') {
      var result = cardRoot.querySelector('.ei-outcome-result').value.trim();
      if (result.length < 8) throw new Error('Describe what happened after the action.');
      var lesson = cardRoot.querySelector('.ei-outcome-lesson').value.trim();
      if (lesson.length < 8) throw new Error('Record a lesson supported by the observed outcome.');
      var body = { result: result, lesson: lesson, reason: reason, sourceUpdateId: cardRoot.querySelector('.ei-outcome-update').value,
        actualFinishDate: cardRoot.querySelector('.ei-outcome-date').value,
        actualDaysSaved: cardRoot.querySelector('.ei-outcome-days').value };
      return post('/api/execution-actions/' + encodeURIComponent(id) + '/outcome', body)
        .then(function () { return refresh('Outcome recorded; project forecast and history refreshed from the measured result.'); });
    }
  }
  function bind(root) {
    root.onclick = function (event) {
      var el = event.target.closest('[data-action]'); if (!el) return;
      Promise.resolve().then(function () { return action(el.getAttribute('data-action'), el.getAttribute('data-id'), root); })
        .catch(function (error) { S.toast(error.message || 'Could not complete this action.', 'error'); });
    };
    root.onsubmit = function (event) {
      var form = event.target.closest('[data-form]'); if (!form) return;
      event.preventDefault();
      var kind = form.getAttribute('data-form'), body = values(form), request;
      if (kind === 'scenario') {
        body.daysSaved = Number(body.daysSaved);
        body.resources = { currentManpower: body.currentManpower, additionalManpower: body.additionalManpower,
          assumedProductivityChangePct: body.assumedProductivityChangePct, additionalEquipment: body.additionalEquipment,
          materialStatus: body.materialStatus, parallelActivityId: body.parallelActivityId };
        delete body.currentManpower; delete body.additionalManpower; delete body.assumedProductivityChangePct;
        delete body.additionalEquipment; delete body.materialStatus; delete body.parallelActivityId;
        request = post('/api/recovery-scenarios', body).then(function (result) {
          return refresh(result.scenario && !result.scenario.scenarioFinish
            ? 'Insufficient schedule data for a finish estimate; the reason and assumptions were retained. No schedule actuals changed.'
            : 'Recovery estimate saved. It requires a planner decision and changed no schedule actuals.');
        });
      } else {
        request = post('/api/recommendations/generate', body).then(function (result) { return refresh(result.count ? result.count + ' recommendation(s) saved for planner review.' : 'No new recommendations; existing open proposals were retained.'); });
      }
      request.then(function () { form.reset(); }).catch(function (error) { S.toast(error.message || 'Could not save this proposal.', 'error'); });
    };
  }
  S.registerView('execution', { eyebrow: heading[0], title: heading[1], description: heading[2] }, render, bind);
})();
