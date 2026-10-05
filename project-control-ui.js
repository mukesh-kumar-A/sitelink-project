/* Project control screens for the existing SiteLink application. */
(function () {
  'use strict';
  var S = window.SiteLinkControl;
  if (!S) return;
  var data = null, loadedAt = 0, busy = false, draftAnalysis = null;
  var labels = {
    plans: ['PLANNING', 'Plans & baselines', 'Review plan versions before changing the working schedule.'],
    areas: ['PROJECT STRUCTURE', 'Project areas', 'Organize activities by work area and sub-area.'],
    today: ['FIELD EXECUTION', 'Today & work log', 'Submit evidence from site. Planner approval is required before actuals change.'],
    risks: ['PROJECT CONTROLS', 'Risks & issues', 'Record and track project risks against the synthetic schedule.']
  };
  function esc(x) { return S.escape(x == null ? '' : String(x)); }
  function user() { return S.getUser() || {}; }
  function projectId() { return S.getProjectId() || ''; }
  function canPlan() { return ['Admin', 'Project Manager', 'Planner'].indexOf(user().role) >= 0; }
  function canReview() { return ['Admin', 'Project Manager', 'Planner'].indexOf(user().role) >= 0; }
  function canSubmit() { return ['Admin', 'Project Manager', 'Planner', 'Supervisor', 'Contractor'].indexOf(user().role) >= 0; }
  function load(force) {
    if (!force && data && Date.now() - loadedAt < 8000) return Promise.resolve(data);
    if (busy) return Promise.resolve(data);
    busy = true;
    return S.api('/api/project-control?projectId=' + encodeURIComponent(projectId()), 'GET').then(function (v) {
      data = v; loadedAt = Date.now(); return v;
    }).catch(function (e) { S.toast(e.message || 'Could not load project controls.', 'error'); return data; })
      .finally(function () { busy = false; S.rerender(); });
  }
  function card(title, body, actions) { return '<section class="pc-card"><div class="pc-card-head"><h2>' + title + '</h2>' + (actions || '') + '</div>' + body + '</section>'; }
  function reasonField() { return '<label class="field"><span>Decision reason (required)</span><input name="reason" required minlength="8" maxlength="500" placeholder="Why is this change needed?"></label>'; }
  function btn(action, text, cls, value) { return '<button type="button" class="button ' + (cls || 'button-secondary') + '" data-action="' + action + '"' + (value ? ' data-id="' + esc(value) + '"' : '') + '>' + text + '</button>'; }
  function options(items, selected, empty) { return (empty ? '<option value="">' + esc(empty) + '</option>' : '') + (items || []).map(function (x) { return '<option value="' + esc(x.id) + '" ' + (x.id === selected ? 'selected' : '') + '>' + esc(x.id + ' · ' + x.name) + '</option>'; }).join(''); }
  function renderPlans() {
    if (!data) { load(); return '<div class="pc-loading">Loading plan history…</div>'; }
    var activities = data.activities || [];
    var head = '<div class="pc-toolbar"><div><strong>Working activities: ' + activities.length + '</strong><small>Only approved baseline versions replace planned schedule fields.</small></div>' + (canPlan() ? btn('upload-plan', '＋ Upload plan version', 'button-primary') : '') + '</div>';
    var baselines = (data.baselines || []).map(function (b) { return '<div class="pc-row"><div><strong>Baseline v' + esc(b.version) + '</strong><small>Approved ' + esc(b.approvedAt || '') + ' · ' + esc(b.activityCount) + ' activities</small></div><span class="pc-pill good">Approved</span></div>'; }).join('') || '<p class="pc-empty">No approved baseline snapshot yet. Upload and approve a plan version to establish one.</p>';
    var plans = (data.plans || []).map(function (p) {
      var parsed = p.analysis || {}, rows = Array.isArray(parsed) ? parsed : (Array.isArray(parsed.activities) ? parsed.activities : []);
      return '<article class="pc-plan"><div class="pc-row"><div><strong>Version ' + esc(p.version) + ' · ' + esc(p.fileName) + '</strong><small>Uploaded ' + esc(p.createdAt || '') + ' · ' + esc(p.uploadedBy || '') + '</small></div><span class="pc-pill">' + esc(p.analysisStatus) + '</span></div>' +
        (parsed.warning ? '<p class="pc-note">' + esc(parsed.warning) + '</p>' : '') +
        (p.transcription ? '<details class="pc-details"><summary>Review extracted source text</summary><pre>' + esc(p.transcription) + '</pre></details>' : '') +
        (!p.approvedAt && canPlan() ? '<div class="pc-actions">' + btn('analyze-plan', 'Analyze plan', 'button-secondary', p.id) + (rows.length ? btn('approve-plan', 'Review & approve baseline', 'button-primary', p.id) : '') + '</div>' : '') +
        (rows.length ? '<details class="pc-details"><summary>Review and edit extracted activities · ' + rows.length + '</summary><p>Check dates, IDs and names against the imported file. Edit the structured rows before approving.</p><textarea class="pc-json" data-plan-rows="' + esc(p.id) + '" rows="12" spellcheck="false">' + esc(JSON.stringify(rows, null, 2)) + '</textarea></details>' : '') + '</article>';
    }).join('') || '<p class="pc-empty">No plan versions uploaded.</p>';
    return '<div class="pc-grid"><div class="pc-main">' + card('Plan versions', head + plans) + '</div><aside>' + card('Approved baseline history', baselines) + card('Current progress', '<strong class="pc-metric">' + esc((data.progress || {}).actual) + '%</strong><p>' + esc((data.progress || {}).weighting || '') + '</p><small>' + esc((data.progress || {}).activityCount || 0) + ' activities contribute.</small>') + '</aside></div>';
  }
  function renderAreas() {
    if (!data) { load(); return '<div class="pc-loading">Loading project areas…</div>'; }
    var tree = (data.areas || []).filter(function (a) { return a.level === 'area'; }).map(function (a) {
      var kids = (data.areas || []).filter(function (b) { return b.parentId === a.id; });
      return '<article class="pc-area"><div><strong>' + esc(a.name) + '</strong><small>' + esc(a.code || 'Work area') + '</small></div><ul>' + (kids.map(function (k) { return '<li>' + esc(k.name) + '</li>'; }).join('') || '<li class="pc-muted">No sub-areas</li>') + '</ul><small>' + ((data.areaSummary || []).filter(function (x) { return x.id === a.id; }).map(function (x) { return x.activityCount + ' activities · ' + x.progress + '% actual · ' + x.condition; })[0] || 'No linked activities') + '</small></article>';
    }).join('') || '<p class="pc-empty">No areas are defined yet.</p>';
    var form = canPlan() ? '<form class="pc-form" data-form="area"><h3>Add to project hierarchy</h3><div class="pc-fields"><label>Type<select name="level"><option value="area">Area</option><option value="sub_area">Sub-area</option></select></label><label>Parent area<select name="parentId"><option value="">Choose for sub-area</option>' + (data.areas || []).filter(function (a) { return a.level === 'area'; }).map(function (a) { return '<option value="' + esc(a.id) + '">' + esc(a.name) + '</option>'; }).join('') + '</select></label><label>Name<input name="name" required maxlength="120"></label></div>' + reasonField() + '<button class="button button-primary">Add area</button></form>' : '';
    return '<div class="pc-grid"><div class="pc-main">' + card('Area hierarchy', '<div class="pc-area-grid">' + tree + '</div>') + '</div><aside>' + card('Manage hierarchy', form || '<p class="pc-empty">Your role can view project areas.</p>') + '</aside></div>';
  }
  function renderToday() {
    if (!data) { load(); return '<div class="pc-loading">Loading daily work log…</div>'; }
    var t = data.today || {}, summary = '<div class="pc-kpis"><div><strong>' + esc(t.plannedActivities) + '</strong><small>Planned today</small></div><div><strong>' + esc(t.updatesSubmitted) + '</strong><small>Updates received</small></div><div><strong>' + esc(t.updatesApproved) + '</strong><small>Approved</small></div><div><strong>' + esc(t.missingUpdates) + '</strong><small>Cadence gaps</small></div></div>';
    var pending = (data.dailyUpdates || []).filter(function (u) { return u.status === 'Submitted'; }).map(function (u) {
      var warning = (u.warnings || []).map(function (w) { return '<li>' + esc(w) + '</li>'; }).join('');
      return '<article class="pc-update"><div class="pc-row"><div><strong>' + esc(u.activityId) + ' · ' + esc(u.workDate) + '</strong><small>' + esc(u.submittedBy) + ' · ' + esc(u.condition || 'Condition not stated') + '</small></div><span class="pc-pill warn">Needs planner review</span></div><p>' + esc(u.workCompleted || u.text) + '</p><div class="pc-facts"><span>Progress: ' + esc(u.progress) + '%</span><span>Quantity: ' + esc(u.quantityCompleted == null ? '—' : u.quantityCompleted) + '</span><span>People: ' + esc(u.manpower || 0) + '</span></div>' + (warning ? '<ul class="pc-warnings">' + warning + '</ul>' : '') + (u.attachments || []).map(function (a) { return '<a class="table-link" target="_blank" href="/api/documents/' + encodeURIComponent(a.id) + '/download">Evidence: ' + esc(a.fileName) + '</a>'; }).join(' ') + (canReview() ? '<div class="pc-review"><input class="input" data-review-reason="' + esc(u.id) + '" minlength="8" placeholder="Decision reason (required)">' + btn('review-update', 'Approve', 'button-primary', u.id) + btn('reject-update', 'Reject', 'button-secondary', u.id) + '</div>' : '') + '</article>';
    }).join('') || '<p class="pc-empty">No submitted daily updates are waiting for review.</p>';
    var form = canSubmit() ? '<form class="pc-form" data-form="daily"><h3>Supervisor daily work entry</h3><div class="pc-fields"><label>Schedule activity<select name="activityId" required><option value="">Select an activity</option>' + (data.activities || []).map(function (a) { return '<option value="' + esc(a.id) + '">' + esc(a.id + ' · ' + a.name) + '</option>'; }).join('') + '</select></label><label>Work / event date<input type="date" name="workDate" required></label><label>Event type<select name="eventStatus"><option value="progress">Progress update</option><option value="started">Activity started</option><option value="completed">Activity finished</option></select></label><label>Event time, if known<input type="time" name="eventTime"></label><label>Progress %<input type="number" name="progress" min="0" max="100" step="0.1" required></label><label>Completed quantity<input type="number" name="quantityCompleted" min="0" step="0.01"></label><label>People on task<input type="number" name="manpower" min="0" max="10000" step="1"></label><label>Condition<select name="condition"><option value="">Not stated</option><option>On track</option><option>At risk</option><option>Delayed</option><option>Blocked</option><option>Completed</option></select></label><label>Materials<select name="materialAvailability"><option>Unknown</option><option>Available</option><option>Partially available</option><option>Pending</option></select></label><label>Equipment used<input name="equipmentUsed" maxlength="500"></label></div><label>Work completed / typed field report<textarea name="workCompleted" required maxlength="5000" rows="4" placeholder="Describe the work completed. Include line tag, location, event date, progress and blockers when known."></textarea></label><div class="pc-actions"><button type="button" class="button button-secondary" data-action="analyze-daily">Analyze with Time Agent</button><label class="button button-secondary pc-file-label">Attach evidence<input type="file" data-file="daily" multiple accept=".pdf,.png,.jpg,.jpeg,.tif,.tiff,.csv,.xlsx,.txt"></label></div><div data-analysis></div><div class="pc-fields"><label>Issues / blockers<textarea name="issues" rows="2"></textarea></label><label>Delay reason<textarea name="delayReason" rows="2"></textarea></label><label>Safety observation<textarea name="safetyObservation" rows="2"></textarea></label><label>Notes<textarea name="notes" rows="2"></textarea></label></div><div data-attachments></div><button class="button button-primary">Submit for planner review</button></form>' : '<p class="pc-empty">Your role does not have permission to submit updates.</p>';
    return '<div class="pc-grid"><div class="pc-main">' + card('Today · ' + esc(t.date), summary) + card('Supervisor work entry', form) + card('Planner review queue', pending) + '</div><aside>' + card('Progress method', '<strong>' + esc((data.progress || {}).actual) + '% weighted progress</strong><p>' + esc((data.progress || {}).weighting || 'No schedule values') + '</p><small>From ' + esc((data.progress || {}).activityCount || 0) + ' activities. Submitted work has no schedule effect until approved.</small>') + card('Recent work history', (data.dailyUpdates || []).slice(0, 8).map(function (u) { return '<div class="pc-row"><div><strong>' + esc(u.activityId) + ' · ' + esc(u.workDate) + '</strong><small>' + esc(u.workCompleted || u.text) + '</small></div><span class="pc-pill ' + (u.status === 'Approved' ? 'good' : u.status === 'Rejected' ? '' : 'warn') + '">' + esc(u.status) + '</span></div>'; }).join('') || '<p class="pc-empty">No submitted updates yet.</p>') + '</aside></div>';
  }
  function renderRisks() {
    if (!data) { load(); return '<div class="pc-loading">Loading risks…</div>'; }
    var risks = (data.risks || []).map(function (r) {
      var controls = canPlan() && !['Resolved', 'Closed'].includes(r.status) ? '<div class="pc-actions"><select data-risk-status="' + esc(r.id) + '"><option>Open</option><option ' + (r.status === 'Monitoring' ? 'selected' : '') + '>Monitoring</option><option>Mitigated</option><option>Resolved</option><option>Closed</option></select><input class="input" data-risk-reason="' + esc(r.id) + '" placeholder="Reason (required)"><button type="button" class="button button-secondary" data-action="change-risk" data-id="' + esc(r.id) + '">Save status</button></div>' : '';
      return '<article class="pc-update"><div class="pc-row"><div><strong>' + esc(r.title) + '</strong><small>' + esc(r.activityId || 'Project-level') + ' · owner ' + esc(r.owner || 'Unassigned') + ' · due ' + esc(r.dueDate || '—') + '</small></div><span class="pc-pill ' + (r.severity === 'Critical' || r.severity === 'High' ? 'warn' : '') + '">' + esc(r.severity) + ' · ' + esc(r.status) + '</span></div><p>' + esc(r.description) + '</p><small>Mitigation: ' + esc(r.mitigation || 'Not recorded') + '</small>' + controls + '</article>';
    }).join('') || '<p class="pc-empty">No project risks or issues are recorded.</p>';
    var form = canPlan() ? '<form class="pc-form" data-form="risk"><h3>Record risk or issue</h3><div class="pc-fields"><label>Title<input name="title" required maxlength="140"></label><label>Severity<select name="severity"><option>Low</option><option selected>Medium</option><option>High</option><option>Critical</option></select></label><label>Probability<select name="probability"><option>Low</option><option selected>Medium</option><option>High</option></select></label><label>Owner<input name="owner" maxlength="100"></label><label>Due date<input name="dueDate" type="date"></label><label>Linked activity<select name="activityId"><option value="">Project-level</option>' + (data.activities || []).map(function (a) { return '<option value="' + esc(a.id) + '">' + esc(a.id + ' · ' + a.name) + '</option>'; }).join('') + '</select></label></div><label>Description<textarea name="description" required maxlength="3000" rows="3"></textarea></label><label>Mitigation<textarea name="mitigation" maxlength="3000" rows="2"></textarea></label>' + reasonField() + '<button class="button button-primary">Save risk</button></form>' : '';
    var milestoneForm = canPlan() ? '<form class="pc-form" data-form="milestone"><h3>Add milestone</h3><label>Name<input name="name" maxlength="160" required></label><label>Planned date<input type="date" name="plannedDate" required></label><label>Linked activity<select name="activityId"><option value="">Project-level milestone</option>' + (data.activities || []).map(function (a) { return '<option value="' + esc(a.id) + '">' + esc(a.id + ' · ' + a.name) + '</option>'; }).join('') + '</select></label>' + reasonField() + '<button class="button button-primary">Save milestone</button></form>' : '';
    return '<div class="pc-grid"><div class="pc-main">' + card('Project risk register', risks) + '</div><aside>' + card('New risk or issue', form || '<p class="pc-empty">Your role can view risks.</p>') + card('Milestones', (data.milestones || []).map(function (m) { return '<div class="pc-row"><div><strong>' + esc(m.name) + '</strong><small>Planned ' + esc(m.plannedDate) + (m.actualDate ? ' · Actual ' + esc(m.actualDate) : '') + '</small></div><span class="pc-pill ' + (m.status === 'Complete' ? 'good' : '') + '">' + esc(m.status) + '</span></div>'; }).join('') || '<p class="pc-empty">No milestones recorded.</p>') + milestoneForm + '</aside></div>';
  }
  function formValues(form) { var o = {}; new FormData(form).forEach(function (v, k) { o[k] = v; }); return o; }
  function send(path, method, body) { body = body || {}; body.projectId = projectId(); return S.api(path, method || 'POST', body).then(function (r) { S.toast('Saved successfully.'); return load(true).then(function () { return r; }); }); }
  function fileBase64(file) { return new Promise(function (resolve, reject) { var reader = new FileReader(); reader.onload = function () { resolve(String(reader.result).split(',')[1]); }; reader.onerror = function () { reject(new Error('Could not read selected file.')); }; reader.readAsDataURL(file); }); }
  function upload(file, category, reason, extra) { return fileBase64(file).then(function (b64) { return send('/api/documents', 'POST', Object.assign({ fileName: file.name, fileBase64: b64, category: category, reason: reason || 'Uploaded as supporting evidence for a submitted site record.' }, extra || {})); }); }
  function submitDaily(form) {
    var values = formValues(form), files = Array.from(form.querySelector('[data-file="daily"]').files || []), analysis = draftAnalysis;
    if (files.length > 20) return Promise.reject(new Error('Attach no more than 20 files.'));
    var reason = 'Supervisor daily work entry with supporting source evidence.';
    return Promise.all(files.map(function (f) { return upload(f, 'daily_update', reason, { activityId: values.activityId }); })).then(function (docs) {
      values.projectId = projectId(); values.attachments = docs.map(function (d) { return d.id; });
      if (analysis) values.aiAnalysis = analysis;
      return S.api('/api/daily-updates', 'POST', values);
    }).then(function () { draftAnalysis = null; S.toast('Update submitted. It will not change schedule actuals until a planner approves it.'); form.reset(); return load(true); });
  }
  function act(action, id, root) {
    if (action === 'choose-candidate') { var activitySelect = root.querySelector('[data-form="daily"] [name="activityId"]'); if (activitySelect) { activitySelect.value = id; S.toast('Suggested activity selected for the draft. Submission still requires review.'); } return; }
    if (action === 'upload-plan') { document.getElementById('planFileInput').click(); return; }
    if (action === 'analyze-plan') return send('/api/plans/' + encodeURIComponent(id) + '/analyze', 'POST', {}).then(function () { return load(true); });
    if (action === 'approve-plan') {
      var box = root.querySelector('[data-plan-rows="' + CSS.escape(id) + '"]');
      if (!box) throw new Error('Open the extracted activity review first.');
      var rows; try { rows = JSON.parse(box.value); } catch (_) { throw new Error('Fix the activity JSON before approving this baseline.'); }
      var reason = window.prompt('Reason for approving this plan baseline (at least 8 characters):');
      if (!reason || reason.trim().length < 8) throw new Error('Baseline approval needs a reason of at least 8 characters.');
      return send('/api/plans/' + encodeURIComponent(id) + '/approve', 'POST', { activities: rows, reason: reason.trim() }).then(function () { return load(true); });
    }
    if (action === 'review-update' || action === 'reject-update') {
      var field = root.querySelector('[data-review-reason="' + CSS.escape(id) + '"]'), reasonText = field ? field.value.trim() : '';
      if (reasonText.length < 8) throw new Error('Enter a decision reason of at least 8 characters.');
      return send('/api/daily-updates/' + encodeURIComponent(id) + '/review', 'POST', { status: action === 'review-update' ? 'Approved' : 'Rejected', reason: reasonText });
    }
    if (action === 'change-risk') {
      var select = root.querySelector('[data-risk-status="' + CSS.escape(id) + '"]'), why = root.querySelector('[data-risk-reason="' + CSS.escape(id) + '"]');
      if (!why || why.value.trim().length < 8) throw new Error('Enter a risk change reason of at least 8 characters.');
      return send('/api/risks/' + encodeURIComponent(id), 'PATCH', { status: select.value, reason: why.value.trim() });
    }
    if (action === 'analyze-daily') {
      var form = root.querySelector('[data-form="daily"]'), vals = formValues(form);
      if (!vals.workCompleted.trim()) throw new Error('Enter a field report before analysis.');
      return S.api('/api/daily-updates/analyze', 'POST', Object.assign({ projectId: projectId(), text: vals.workCompleted, workDate: vals.workDate, activityId: vals.activityId }, vals)).then(function (r) {
        draftAnalysis = r;
        var target = form.querySelector('[data-analysis]'), facts = r.facts || {}, candidates = r.candidates || r.matches || [];
        var candidateHtml = (r.candidates || []).map(function (c) { return '<div class="pc-candidate"><div><strong>' + esc(c.activity_id) + ' · ' + esc(c.activity_name) + '</strong><small>' + esc((c.reasons || []).join(' · ')) + '</small></div><span class="pc-pill">' + esc(Math.round((c.score || 0) * 100)) + '% heuristic</span><button type="button" class="button button-secondary" data-action="choose-candidate" data-id="' + esc(c.activity_id) + '">Select</button></div>'; }).join('') || '<p class="pc-note">No likely schedule match. Select an activity manually or leave for planner triage.</p>';
        var evidenceHtml = (facts.evidence || []).map(function (e) { return '<li><strong>' + esc(e.field || 'source') + ':</strong> ' + esc(e.value || '') + (e.quote ? ' <small>Source text: “' + esc(e.quote) + '”</small>' : '') + '</li>'; }).join('');
        var needsDate = !vals.workDate;
        target.innerHTML = '<div class="pc-analysis"><strong>Time Agent · ' + esc(r.provider || 'fallback extraction') + ' · heuristic confidence ' + esc(r.confidence == null ? 'not scored' : Math.round(r.confidence * 100) + '%') + '</strong><p>Extracted facts need supervisor/planner verification; scores are heuristic, not calibrated.</p>' + (needsDate ? '<p class="pc-note">Clarification required: enter the date this work happened. The received/submission date is not substituted.</p>' : '') + '<div class="pc-facts">' + Object.keys(facts).filter(function (k) { return k !== 'evidence' && facts[k] != null && facts[k] !== ''; }).map(function (k) { return '<span>' + esc(k) + ': ' + esc(typeof facts[k] === 'object' ? JSON.stringify(facts[k]) : facts[k]) + '</span>'; }).join('') + '</div>' + (evidenceHtml ? '<details class="pc-details"><summary>Source evidence for extracted fields</summary><ul>' + evidenceHtml + '</ul></details>' : '') + (r.clarifications || []).map(function (x) { return '<p class="pc-note">Clarify: ' + esc(x) + '</p>'; }).join('') + '<h4>Up to three ranked schedule activities</h4>' + candidateHtml + '</div>';
        return r;
      });
    }
  }
  function bind(root) {
    root.oninput = function (event) { if (event.target.matches('[data-form="daily"] [name="workCompleted"]')) { draftAnalysis = null; var box = root.querySelector('[data-analysis]'); if (box) box.innerHTML = '<small>Report text changed. Run Time Agent again to refresh extracted facts and matches.</small>'; } };
    root.onchange = function (event) { if (event.target.matches('[data-form="daily"] [name="eventStatus"]') && event.target.value === 'completed') { var progress = root.querySelector('[data-form="daily"] [name="progress"]'); if (progress) progress.value = '100'; } };
    root.onclick = function (event) {
      var el = event.target.closest('[data-action]'); if (!el) return;
      var action = el.getAttribute('data-action'), id = el.getAttribute('data-id');
      Promise.resolve().then(function () { return act(action, id, root); }).catch(function (e) { S.toast(e.message || 'Action failed.', 'error'); });
    };
    root.onsubmit = function (event) {
      var form = event.target.closest('[data-form]'); if (!form) return;
      event.preventDefault();
      var kind = form.getAttribute('data-form'), values = formValues(form);
      var work;
      if (kind === 'daily') work = submitDaily(form);
      else if (kind === 'area') work = send('/api/areas', 'POST', values).then(function () { form.reset(); });
      else if (kind === 'risk') work = send('/api/risks', 'POST', values).then(function () { form.reset(); });
      else if (kind === 'milestone') work = send('/api/milestones', 'POST', values).then(function () { form.reset(); });
      Promise.resolve(work).catch(function (e) { S.toast(e.message || 'Could not save this entry.', 'error'); });
    };
    var planInput = document.getElementById('planFileInput');
    if (planInput) planInput.onchange = function () {
      var file = planInput.files && planInput.files[0]; if (!file) return;
      var reason = window.prompt('Reason for uploading this plan version:');
      if (!reason || reason.trim().length < 8) { planInput.value = ''; S.toast('Plan upload needs a reason of at least 8 characters.', 'error'); return; }
      fileBase64(file).then(function (b64) { return S.api('/api/plans', 'POST', { projectId: projectId(), fileName: file.name, fileBase64: b64, reason: reason.trim() }); })
        .then(function () { S.toast('Plan version uploaded. Analyze and review it before approval.'); planInput.value = ''; return load(true); })
        .catch(function (e) { planInput.value = ''; S.toast(e.message || 'Plan upload failed.', 'error'); });
    };
  }
  Object.keys(labels).forEach(function (key) {
    var meta = labels[key];
    S.registerView(key, { eyebrow: meta[0], title: meta[1], description: meta[2] }, function () {
      if (!data || Date.now() - loadedAt > 8000) { load(); return '<div class="pc-loading">Loading project workspace…</div>'; }
      return key === 'plans' ? renderPlans() : key === 'areas' ? renderAreas() : key === 'today' ? renderToday() : renderRisks();
    }, bind);
  });
})();
