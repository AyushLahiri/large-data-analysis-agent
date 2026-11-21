/**
 * Find a page element by ID.
 *
 * Inputs: id: element ID.
 * Returns: Matching DOM element, or null.
 * Details: Shared shorthand used by both UI scripts.
 */
const $ = id => document.getElementById(id);
let token = '', projectId = '', state = null, busy = false, polling = false, submitting = false, liveMetrics = [];
let renameFolder = '', deleteTarget = null;
let nameMode = 'new', uploadFolder = '', uploadPrimary = false, uploadDefault = 'columns';
let selectedFiles = [], chatSignature = '', treeSignature = '', phaseSignature = '', atlasSignature = '', formId = '';
let activeView = 'ingest', publicationCount = 0;
let setupFolder = '', setupQueue = [], setupIndex = 0, setupDraft = null, reuseInfo = null, folderDraft = null;
let setupMode = 'questions', reviewAfterTags = false;
const questionnaire = new Map(), editedAnswers = new Set();
const drafts = new Map(), answers = new Map(), expanded = new Map();
const statuses = {draft:'Ready to begin', running:'In progress', stopping:'Stopping after current replies', awaiting_user:'Your input needed', ready:'Ready to prepare', complete:'Preparation complete', failed:'Needs attention', interrupted:'Ready to retry', stopped:'Stopped', blocked:'Preparation paused', deleting:'Deletion incomplete — retry Delete project'};
const phaseNames = {reading_instructions:'Reading instructions', candidate_keys:'Record identifiers', combine_datasets:'Combining files', sql_types:'Column types', split_and_name:'Tables & names', loading_artefacts:'Prepared outputs'};

const datasetQuestions = [
  ['description', 'Describe this dataset and the population it covers', 'Include who or what is covered. For example: all surveyed households, including rural areas.'],
  ['time_geography', 'Time period and location / geography', 'For example: January–December 2025, Kenya. Write “not applicable” if needed.'],
  ['source', 'Dataset source', 'For example: World Bank, DHS or UNICEF. Write “self” for data you collected.'],
  ['unit', 'What does each row represent?', 'For example: one person, one household, or one household per survey wave. Mention repeated observations.']
];
const continuationWarning = 'Only add rows or columns here when this file continues the main dataset. Please do not add a new dataset, such as a supplemental or geospatial dataset, to this folder. Finish this folder’s form first, then add that dataset in a new folder in the same project.';
const descriptionAdvice = 'The better you describe the data, the higher the chance of successful ingestion.';
/**
 * Get a folder's current display name.
 *
 * Inputs: key: stable folder key; reads project state.
 * Returns: Display name, falling back to the key.
 * Details: Keeps labels readable while API and agent references use stable keys.
 */
function folderLabel(key) { return state?.folders[key]?.display_name || key; }
/**
 * Find the folders already published to Atlas.
 *
 * Inputs: Reads the current project publication.
 * Returns: Set of published folder keys.
 * Details: Used by rendering, context editing and upload controls to protect published
 *     data.
 */
function lockedFolders() { return new Set((state?.atlas?.data || []).map(table => table.folder)); }

/** Lock submitted forms until ingestion stops; published folders always stay locked. */
function frozenFolders() {
  if (['stopped','failed','interrupted','blocked'].includes(state?.status) || state?.ingestion_changed) return lockedFolders();
  return new Set([...lockedFolders(), ...Object.entries(state?.folders||{}).filter(([,folder])=>folder.setup_reviewed||folder.setup_complete).map(([name])=>name)]);
}

/**
 * Create a button with shared asynchronous error handling.
 *
 * Inputs: text: label; handler: callback; className: button classes.
 * Returns: Unattached button element.
 * Details: Click runs handler through a promise and sends failures to fail; the button
 *     does not submit forms by default.
 */
function button(text, handler, className='quiet') {
  const b = el('button', text, className); b.type = 'button';
  b.onclick = () => Promise.resolve().then(handler).catch(fail);
  return b;
}
/**
 * Show a request or action failure in the page notice.
 *
 * Inputs: error: object with a message.
 * Returns: Nothing; updates and reveals the notice.
 * Details: Shared error callback for api callers and button actions.
 */
function fail(error) { $('notice').textContent = error.message; $('notice').hidden = false; }
/**
 * Hide the current page-level error notice.
 *
 * Inputs: No arguments; uses the notice element.
 * Returns: Nothing.
 * Details: Called before a new action or project switch; inline dialog errors have
 *     separate elements.
 */
function clearNotice() { $('notice').hidden = true; }
/**
 * Build an API path for the selected project.
 *
 * Inputs: suffix: project-relative endpoint.
 * Returns: URL path string; throws if no project is selected.
 * Details: Shared by ingestion and analysis actions.
 */
function endpoint(suffix) {
  if (!projectId) throw Error('Choose or create a project first.');
  return `/api/projects/${projectId}/${suffix}`;
}
/**
 * Send a local API request and return its JSON response.
 *
 * Inputs: path: URL; body: omitted for GET, FormData for upload, or JSON data for POST.
 * Returns: Promise resolving to parsed JSON; rejects with the server error or a non-JSON
 *     response message.
 * Details: Adds the app token to writes, sets JSON headers when appropriate and checks
 *     HTTP status.
 */
async function api(path, body) {
  const options = body === undefined ? {} : {method:'POST', headers:{'X-V5-Token':token}, body};
  if (body !== undefined && !(body instanceof FormData)) {
    options.headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const isJson = response.headers.get('content-type')?.includes('application/json');
  if (!isJson) throw Error(`The server could not complete this request (${response.status}). Please try again.`);
  const result = await response.json();
  if (!response.ok) throw Error(result.error || 'The request could not be completed.');
  return result;
}
/**
 * Format a byte count for the upload list.
 *
 * Inputs: bytes: file size.
 * Returns: Short B, KB or MB string.
 * Details: Used by material and selected-file rendering; values are display labels only.
 */
function size(bytes) { return bytes < 1024 ? `${bytes} B` : bytes < 1048576 ? `${(bytes/1024).toFixed(1)} KB` : `${(bytes/1048576).toFixed(1)} MB`; }
/**
 * Create a collapsible section whose open state survives rendering.
 *
 * Inputs: key: state key; title: heading; className: classes; initiallyOpen: initial
 *     fallback.
 * Returns: Details element with its summary already attached.
 * Details: Reads/writes the expanded map on toggle; shared by materials, messages and
 *     analysis panels.
 */
function disclosure(key, title, className='', initiallyOpen=false) {
  const d = el('details', undefined, className); d.open = expanded.get(key) ?? initiallyOpen;
  d.append(el('summary', title)); d.addEventListener('toggle', () => expanded.set(key, d.open));
  return d;
}
/**
 * Append an editable note backed by the local draft map.
 *
 * Inputs: parent: container; label/key: caption and draft ID; saved: fallback text;
 *     required: validation flag.
 * Returns: Nothing; appends a labeled textarea and records edits in drafts.
 * Details: Used for project material context. Drafts take precedence over polled server
 *     values until saveNotes succeeds.
 */
function noteField(parent, label, key, saved='', required=false) {
  const wrap = el('div', undefined, 'note-field'), caption = el('label', label);
  const input = el('textarea'); input.rows = 2; input.value = drafts.get(key) ?? saved; input.disabled = busy || submitting;
  input.id = `note-${key}`; caption.htmlFor = input.id; input.required = required;
  input.oninput = () => { if(input.value===saved) drafts.delete(key); else drafts.set(key, input.value); $('save-state').textContent = drafts.size?'Unsaved notes':'Notes saved with this project'; renderRetry(); };
  wrap.append(caption, input); parent.append(wrap);
}
/**
 * Refresh the project selector while retaining its selection.
 *
 * Inputs: Reads projectId; no arguments.
 * Returns: Promise resolving with no value; replaces selector options.
 * Details: Calls GET /api/projects through api, then restores the selected ID.
 */
async function listProjects() {
  const projects = await api('/api/projects');
  $('projects').replaceChildren(new Option('Choose a project', ''));
  for (const p of projects) $('projects').add(new Option(p.name, p.id));
  $('projects').value = projectId;
}
/**
 * Poll the selected project without applying stale responses.
 *
 * When: The polling timer or a completed UI action requests fresh state.
 * Inputs: Reads projectId and the polling guard.
 * Returns: Promise with no value; updates state and calls render only for the same
 *     project.
 * Flow: Allows one request at a time and ignores results/errors from a project the user
 *     has left.
 */
async function refresh() {
  if (!projectId || polling) return;
  polling = true; const requestedId = projectId;
  try { const result = await api(`/api/projects/${requestedId}`); if (projectId === requestedId) {state = result; render();} }
  catch (error) { if (projectId === requestedId) throw error; }
  finally { polling = false; }
}
/**
 * Clear display caches and unsaved UI state for a project switch.
 *
 * Inputs: Uses shared view state; no arguments.
 * Returns: Nothing; clears drafts, answers, open panels, reply text and analysis view
 *     state.
 * Details: Called after selection changes or a new project is created so previous-project
 *     content cannot carry over.
 */
function resetView() { chatSignature = treeSignature = phaseSignature = atlasSignature = formId = ''; activeView='ingest'; publicationCount=0; window.analysisView?.reset(); drafts.clear(); answers.clear(); expanded.clear(); $('reply').value = ''; $('folder-setup').close(); setupFolder=''; reuseInfo=null; }
/**
 * Refresh the ingestion workspace from project state.
 *
 * When: Initial display, polling or an action changes state.
 * Inputs: Reads state, drafts, submission flags and selected view.
 * Returns: Nothing; updates controls and delegates panel rendering.
 * Flow: Derives readiness/publication/retry states, switches to analysis when a folder is
 *     published, and preserves the active note field while calling the specialized
 *     renderers.
 */
function render() {
  busy = !!state && ['running','awaiting_user','ready','stopping'].includes(state.status);
  const deleting = state?.status === 'deleting';
  const published = lockedFolders();
  if (published.size > publicationCount && !state?.active_folder) activeView = 'analyse';
  if (!published.size) activeView = 'ingest';
  publicationCount = published.size;
  $('workspace-tabs').hidden = !publicationCount;
  $('ingest-workspace').hidden = activeView !== 'ingest';
  $('analyse-workspace').hidden = activeView !== 'analyse';
  $('view-analyse').setAttribute('aria-pressed', String(activeView === 'analyse'));
  $('view-ingest').setAttribute('aria-pressed', String(activeView === 'ingest'));
  $('ingest-indicator').textContent = state?.status === 'awaiting_user' || state?.status === 'ready' ? 'Ingest needs your input' : busy ? 'Ingest is in progress' : '';
  if (activeView === 'analyse') window.analysisView?.refresh();
  $('title').textContent = state?.context.name || 'A little clarity for your data.';
  $('rename').hidden = !state;
  $('rename').disabled = busy || deleting || submitting;
  $('delete-project').hidden = !state;
  $('delete-project').disabled = busy || submitting;
  $('delete-project').title = busy ? 'Stop the conversation before deleting this project.' : 'Delete project';
  $('before-note').disabled = !state || busy || deleting || submitting;
  if (document.activeElement !== $('before-note')) $('before-note').value = drafts.get('before_ingestion') ?? state?.context.before_ingestion ?? ''; 
  $('subtitle').textContent = state ? 'One place to understand your materials and prepare your data.' : 'Start with a dataset. We’ll help you make sense of the rest.';
  const atlas = state?.atlas;
  $('status').textContent = state ? (state.status === 'complete' && atlas?.status === 'published' ? 'Published to Atlas' : statuses[state.status] || state.status) : 'No project selected';
  $('status').classList.toggle('error', ['failed','blocked','deleting'].includes(state?.status));
  const dataFolders = new Set(Object.values(state?.files || {}).filter(file => !file.ignore && ['primary','continuation'].includes(file.role)).map(file => file.folder));
  const publishedFolders = new Set((atlas?.data || []).map(table => table.folder));
  const summaryDue = atlas?.status === 'published' && dataFolders.size && publishedFolders.size === dataFolders.size &&
    [...dataFolders].every(folder => publishedFolders.has(folder)) && (!state.final_summary_path || state.final_summary_release !== atlas.release);
  $('prepare').hidden = state?.status !== 'ready';
  $('prepare').disabled = submitting;
  const savedCodebooks = state?.status === 'complete' && state.handoff_path && dataFolders.size &&
    [...dataFolders].every(folder => state.codebooks?.[folder]?.final && state.preparations?.[folder]?.manifest_path);
  $('publish').hidden = busy || (!summaryDue && (atlas?.status === 'published' || !(state?.atlas_handoff_path || savedCodebooks)));
  $('publish').disabled = submitting;
  $('publish').textContent = summaryDue ? (state.status === 'failed' ? 'Retry project summary' : 'Create project summary') :
    (state?.atlas_handoff_path ? 'Retry Atlas upload' : 'Publish to Atlas');
  $('stop').hidden = !busy;
  $('stop').disabled = submitting || state?.status === 'stopping';
  $('stop').classList.toggle('stopping', state?.status === 'stopping');
  $('stop').setAttribute('aria-busy', String(state?.status === 'stopping'));
  $('stop').title = state?.status === 'stopping' ? 'Waiting for current agents to finish' : 'Stop ingestion';
  for (const id of ['save','dataset','ingest']) $(id).disabled = !state || busy || deleting || submitting;
  const unfinishedFolder=!!state?.active_folder&&!state.folders[state.active_folder]?.setup_reviewed;
  $('dataset').disabled ||= unfinishedFolder;
  if (state && !Object.keys(state.files).length) $('ingest').disabled = true;
  if (published.size && ![...dataFolders].some(folder => !published.has(folder))) $('ingest').disabled = true;
  $('ingest').textContent = 'Ingest materials →';
  $('ingest').hidden = false;
  $('ingest').disabled ||= [...dataFolders].some(folder=>!published.has(folder)&&!state.folders[folder]?.setup_reviewed);
  $('dataset').replaceChildren(el('span','＋'), document.createTextNode('Add new data and related materials'));
  $('dataset').title = unfinishedFolder ? 'Finish this folder’s form before adding another folder.' : '';
  $('file-count').textContent = `${Object.keys(state?.files || {}).length} files`;
  $('save-state').textContent = drafts.size ? 'Unsaved notes' : 'Notes saved with this project';
  $('live-thinking').hidden = state?.status !== 'running';
  $('activity').textContent = state?.activity || 'Reviewing your materials';
  renderRetry(); renderFolderProgress(); renderChat(); renderMaterials(); renderQuestions(); renderReports(); renderAtlas();
}

/** Show checkpoint retry after a stopped run, and explain why changed inputs block it. */
function renderRetry() {
  const paused = !!state?.log_directory && (['stopped','failed','interrupted','blocked'].includes(state.status) || state.ingestion_changed);
  const changed = !!state?.ingestion_changed || drafts.size > 0;
  $('retry-ingestion').hidden = !paused;
  $('retry-ingestion').disabled = busy || submitting || changed;
  $('retry-reason').hidden = !paused || !changed;
  $('retry-reason').textContent = 'Ingestion materials have been updated. Please restart ingestion again.';
  if(paused) $('publish').hidden = true; // The same Retry button resumes publication failures too.
}

/** Keep an existing saved-folder selection informative without starting ingestion here. */
function renderFolderProgress() {
  const panel = $('folder-progress'), folder = state?.active_folder;
  panel.replaceChildren(); panel.hidden = !folder;
  if (!folder) return;
  const reviewed = state.folders[folder]?.setup_reviewed;
  panel.hidden = !reviewed;
  if(panel.hidden) return; // The upload-stage action belongs directly beneath Add files.
  panel.append(el('strong', folderLabel(folder)),el('p','The folder form is saved. Add another folder or choose Ingest materials when ready.','muted small'));
}

/**
 * Render the publication receipt and download links.
 *
 * Inputs: Reads state.atlas, final_summary_path and busy status.
 * Returns: Nothing; updates the Atlas result panel.
 * Details: Skips unchanged receipts, groups physical tables by dataset and uses endpoint
 *     for project downloads.
 */
function renderAtlas() {
  const atlas = state?.atlas;
  const signature = JSON.stringify([atlas, busy, state?.final_summary_path]);
  if (signature === atlasSignature) return;
  atlasSignature = signature;
  const panel = $('atlas-result');
  panel.replaceChildren();
  panel.hidden = !atlas;
  if (!atlas) return;
  panel.append(el('h3', 'Atlas publication'));
  if (atlas.status !== 'published') {
    const message = busy ? 'Uploading prepared files to Atlas…' : 'The Atlas upload is incomplete. Retry from the saved outputs.';
    panel.append(el('p', message, 'muted'));
    if (!atlas.data?.length) return;
  }
  panel.append(el('p', `Database ${atlas.database} · Schema ${atlas.schema}`, 'atlas-location'));
  if (state.final_summary_path) {
    const summary = el('a', 'Download project data summary');
    summary.href = endpoint(`download/${state.final_summary_path}`);
    const link = el('p'); link.append(summary); panel.append(link);
  }
  const datasets = new Map();
  for (const table of atlas.data) {
    if (!datasets.has(table.dataset_id)) datasets.set(table.dataset_id, {name:table.dataset_name, tables:[]});
    datasets.get(table.dataset_id).tables.push(table);
  }
  const data = disclosure('atlas-data', 'Data', 'phase', true);
  for (const dataset of datasets.values()) {
    data.append(el('strong',dataset.name));
    const tables = el('ul');
    for (const table of dataset.tables) tables.append(el('li', `${table.name} · ${table.row_count.toLocaleString()} rows`));
    data.append(tables);
  }
  panel.append(data);
  for (const [key, title] of [['codebook','Codebook'],['information','Information']]) {
    const group = disclosure(`atlas-${key}`, title, 'phase', true);
    const list = el('ul');
    for (const [name, rows] of Object.entries(atlas[key])) list.append(el('li', `${name} · ${rows.toLocaleString()} rows`));
    group.append(list);
    if (key === 'codebook') group.append(el('p', `${atlas.embeddings.toLocaleString()} embeddings · ${atlas.dimensions.toLocaleString()} dimensions · HNSW`, 'muted small'));
    panel.append(group);
  }
  if(atlas.ingested_codebooks?.length) {
    const group=disclosure('atlas-source-codebooks','Ingested source codebooks','phase');
    const list=el('ul');
    for(const source of atlas.ingested_codebooks) list.append(el('li',`${source.filename} → ${source.name}`));
    group.append(list);panel.append(group);
  }
}
/**
 * Recognize messages that belong in progress disclosures.
 *
 * Inputs: m: message record.
 * Returns: Boolean progress classification.
 * Details: Used by renderChat; also supports older messages that have only an agent-name
 *     convention.
 */
function isThinking(m) { return m.kind === 'thinking' || (!m.kind && /^(agent_|data_loader|loader_summarizer|glitch)/.test(m.agent)); }
/**
 * Replace internal file and reviewer IDs in displayed text.
 *
 * Inputs: text: agent message; reads uploaded file metadata.
 * Returns: Text using filenames and a plain reviewer label.
 * Details: Used before formatting ingestion messages; does not change saved message
 *     records.
 */
function publicText(text) {
  let value = text || '';
  for (const file of Object.values(state?.files || {})) value = value.replaceAll(file.file_id, file.filename);
  return value.replace(/agent_[A-Za-z0-9_]+/g, 'a file reviewer');
}
// Chat may contain text from uploaded files. Markdown does not execute embedded HTML.
const chatMarkdown = markdownit({ html: false, breaks: true }).disable('image');
chatMarkdown.renderer.rules.table_open = () => '<div class="analysis-table-wrap"><table class="analysis-table">\n';
chatMarkdown.renderer.rules.table_close = () => '</table></div>\n';
for (const tag of ['th_open', 'td_open']) {
  chatMarkdown.renderer.rules[tag] = (tokens, index, options, env, renderer) => {
    const token = tokens[index];
    const alignment = token.attrGet('style');
    // Table alignment uses CSS classes to comply with the app's content security policy.
    if (alignment) token.attrs = [['class', alignment.replace('text-align:', 'markdown-align-')]];
    return renderer.renderToken(tokens, index, options);
  };
}
/**
 * Render message Markdown with the app's restricted renderer.
 *
 * Inputs: text: message body; className: styling classes.
 * Returns: Unattached message-body element.
 * Details: Shared by both workspaces. chatMarkdown disables embedded HTML and images;
 *     table rules supply compatible wrappers/classes.
 */
function formattedMessage(text, className) {
  const body = el('div', undefined, `${className} formatted-message`);
  body.innerHTML = chatMarkdown.render(text || '');
  return body;
}
/**
 * Build one progress item with optional evidence details.
 *
 * Inputs: message: text and optional title, observations, uncertainties, code or result
 *     payload.
 * Returns: Unattached progress element.
 * Details: Used by ingestion/analysis chat renderers; code/results stay in an expandable
 *     section.
 */
function thinkingStep(message) {
  const step = el('div', undefined, 'thinking-step');
  const filename = message.agent?.match(/ - (.+?) \(/)?.[1];
  step.append(el('strong', message.details?.title || filename || (message.agent === 'execution' ? 'Code & results' : 'Progress update')), formattedMessage(message.text,'thinking-text'));
  const detail = message.details;
  if (detail) {
    for (const key of ['observations','uncertainties']) if (detail[key]?.length) {
      const ul = el('ul'); for (const item of detail[key]) ul.append(el('li', item)); step.append(ul);
    }
    if (detail.code || detail.result) {
      const d = el('details'); d.append(el('summary', message.agent === 'analysis_progress' ? 'View code and result size' : 'View executed code and result'));
      if (detail.code) d.append(el('pre', detail.code));
      if (detail.result) d.append(el('pre', JSON.stringify(detail.result, null, 2)));
      step.append(d);
    }
  }
  return step;
}
/**
 * Render ingestion messages without losing the reader's position.
 *
 * Inputs: Reads project messages, display labels and cached signature.
 * Returns: Nothing; replaces chat content when changed.
 * Details: Groups progress into disclosures and completed-folder segments; stays at the
 *     bottom only if the user was already there.
 */
/**
 * Show one ingestion phase's elapsed time and model cost below its messages.
 *
 * Inputs: index into state.phases; lastTime is the phase's latest message time.
 * Returns: The metrics row. An open phase counts up each second while ingestion runs;
 *     one left open by an interrupted run stops at its last message.
 */
function phaseMetrics(index, lastTime) {
  const phase = state.phases?.[index];
  if (!phase) return '';
  const row = el('div', undefined, 'run-metrics');
  const time = el('span', '', 'run-metrics-value');
  row.append(el('span', phase.folder ? `${phase.label} · ${folderLabel(phase.folder)}` : phase.label, 'run-metrics-label'),
    time, el('span', `$${phase.cost.toFixed(2)}`, 'run-metrics-value'));
  const start = Date.parse(phase.started);
  const end = phase.ended ? Date.parse(phase.ended) : busy ? null : Date.parse(lastTime);
  const update = () => { time.textContent = `⏱︎ ${formatDuration(((end ?? Date.now()) - start) / 1000)}`; };
  update();
  if (end === null) liveMetrics.push(update);
  return row;
}
/** Format seconds as "N s" or "N min N s". */
function formatDuration(total) {
  const seconds = Math.max(0, Math.floor(total)), minutes = Math.floor(seconds / 60);
  return minutes ? `${minutes} min ${seconds % 60} s` : `${seconds} s`;
}
function renderChat() {
  const messages = state?.messages || [];
  const signature = JSON.stringify([messages, state?.phases, busy, messages.filter(m => m.kind === 'folder_complete').map(m => folderLabel(m.details.folder))]);
  if (signature === chatSignature) return; chatSignature = signature;
  const chat = $('chat'), atBottom = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 100;
  const oldScroll = chat.scrollTop; chat.replaceChildren(); liveMetrics = [];
  if (!messages.length) {
    const welcome = el('div', undefined, 'empty-chat');
    welcome.append(el('span','✧','empty-icon'), el('strong', state ? 'Let’s get to know your data.' : 'Every dataset has a story.'),
      el('p', state ? 'Add your materials on the right, then start ingestion. We’ll ask only when your input is needed.' : 'Create a project to bring your files together and begin a conversation.'));
    chat.append(welcome); return;
  }
  let group = null, content = null, segment = document.createDocumentFragment(), phase;
  const lastTime = {};
  for (const m of messages) if (m.phase != null) lastTime[m.phase] = m.time;
  messages.forEach((m, index) => {
    // A phase's time and cost follow its last message, before the next phase starts.
    if (m.phase != null && m.phase !== phase) {
      if (phase != null) segment.append(phaseMetrics(phase, lastTime[phase]));
      phase = m.phase; group = null;
    }
    const completed = m.kind === 'folder_complete';
    if (completed) {
      const folder = disclosure(`chat-folder-${m.id}`, folderLabel(m.details.folder), 'chat-folder');
      folder.append(segment);
      chat.append(folder);
      segment = document.createDocumentFragment();
    }
    if (isThinking(m)) {
      if (!group) {
        group = disclosure(`thinking-${projectId}-${m.id || index}`, 'Thinking…', 'thinking-group');
        group.querySelector('summary').prepend(el('span', undefined, 'thinking-orb'));
        content = el('div', undefined, 'thinking-content'); group.append(content); segment.append(group);
      }
      content.append(thinkingStep(m)); return;
    }
    group = null;
    const user = ['You','user'].includes(m.agent);
    const wrap = el('div', undefined, `message ${user?'user':''} ${m.agent==='app'?'error':''}`);
    wrap.append(el('span', user ? 'You' : 'Workspace', 'author'), user || completed ? el('div',m.text,'bubble') : formattedMessage(publicText(m.text),'bubble')); segment.append(wrap);
  });
  if (phase != null) segment.append(phaseMetrics(phase, lastTime[phase]));
  chat.append(segment);
  chat.scrollTop = atBottom ? chat.scrollHeight : oldScroll;
}
/**
 * Render the upload tree and editable context fields.
 *
 * Inputs: Reads project folders/files, publication status, drafts and busy flags.
 * Returns: Nothing; updates the material/context panels and action buttons.
 * Details: Skips rebuilding focused input fields, preserves disclosures and locks
 *     published folders. noteField keeps unsaved text separate from server state.
 */
function renderMaterials() {
  if (document.activeElement?.matches('#context textarea, #tree textarea, #context input, #tree input')) return;
  const published = lockedFolders();
  const signature = JSON.stringify([state?.context, state?.folders, state?.files, [...frozenFolders()], state?.log_directory, busy, submitting]);
  if (signature === treeSignature) return; treeSignature = signature;
  $('context').replaceChildren(); $('tree').replaceChildren();
  if (!state) { const empty=el('div',undefined,'empty-tree');empty.append(el('strong','A home for your materials'),el('span','Create a project to start uploading.'));$('tree').append(empty);return; }
  noteField($('context'), 'Project description', 'project_description', state.context.description || '');
  for (const [name, folder] of Object.entries(state.folders)) {
    const files = Object.values(state.files).filter(f => f.folder === name)
      .sort((a,b) => Number(b.role === 'primary') - Number(a.role === 'primary'));
    const locked = frozenFolders().has(name);
    const d = disclosure(`folder-${name}`,'','folder',!locked), summary = d.querySelector('summary');summary.className='folder-heading';
    summary.append(el('span',undefined,'folder-icon'),el('span',folderLabel(name)),el('span',published.has(name)?'Published':locked?'Submitted':`${files.length} files`,'folder-count'));
    const rename=button('✎',()=>{},'icon-button folder-rename');rename.setAttribute('aria-label',`Rename folder ${folderLabel(name)}`);
    rename.disabled=locked||busy||submitting;
    rename.onclick=event=>{event.preventDefault();event.stopPropagation();renameFolder=name;openName('folder');};summary.append(rename);
    const removeFolder=button('×',()=>{},'delete-box');removeFolder.setAttribute('aria-label',`Delete folder ${folderLabel(name)}`);
    removeFolder.disabled=busy||submitting;removeFolder.hidden=published.has(name)||(locked&&!!state.log_directory);
    removeFolder.onclick=event=>{event.preventDefault();event.stopPropagation();openDelete({folder:name});};summary.append(removeFolder);
    const children = el('div',undefined,'folder-children');
    if(folder.description) children.append(el('p',folder.description,'folder-description preserved-text'));
    if(folder.summary){const notes=disclosure(`folder-notes-${name}`,'Organisation notes','folder-description');notes.append(el('p',folder.summary));children.append(notes);}
    if(files.length && files.every(f=>f.ignore))children.append(el('p','Set aside for review. These files have not been assigned reliably and will not be loaded.','folder-warning'));
    for(const [label,subset] of [['Dataset & continuations',files.filter(f=>f.role!=='asset')],['Supporting materials',files.filter(f=>f.role==='asset')]]) {
      if(!subset.length)continue;children.append(el('div',label,'branch-label'));
      for(const f of subset){
        const item=disclosure(`file-${f.file_id}`,'',`file-row ${f.role==='asset'?'asset':''} ${f.ignore?'ignored':''}`);
        const head=item.querySelector('summary'), text=el('div');
        const assetLabel = {codebook:'Codebook', report:'Report or presentation', presentation:'Presentation'}[f.asset_kind] || 'Related material';
        text.append(el('div',f.filename,'file-name'),el('div',`${size(f.bytes)} · ${f.ignore?'Excluded':f.role==='primary'?'Primary dataset':f.role==='asset'?assetLabel:`Remaining ${(f.continuation||[]).join(' + ')||'data'}`}`,'file-meta'));
        if(f.duplicates?.length) text.append(el('div','Exact copy already exists','copy-label'));
        head.append(el('span',f.format.toUpperCase().replace('EXCEL','XLS'),'file-icon'),text);
        const remove=button('×',()=>{},'delete-box');remove.setAttribute('aria-label',`Delete ${f.filename}`);remove.disabled=busy||submitting;remove.hidden=locked;
        remove.title=busy?'Stop the conversation before deleting a file.':'Delete file and its notes';
        remove.onclick=event=>{event.preventDefault();event.stopPropagation();openDelete(f);};head.append(remove);
        const detail=el('div',undefined,'file-detail');
        if(f.embedded_codebook_status==='pending')text.append(el('div','Extracting embedded codebook…','file-meta'));
        if(f.embedded_codebook_status==='failed')detail.append(el('p',`Embedded codebook extraction failed: ${f.embedded_codebook_error}`,'folder-warning'));
        if(f.embedded_for)text.append(el('div','Embedded codebook','file-meta'));
        if(f.dataset_context?.description || f.note) detail.append(el('p', f.dataset_context?.description || f.note, 'muted small preserved-text'));
        if(f.duplicates?.length) detail.append(copyDetails(f.duplicates));
        if(!locked && !f.embedded_for && f.role!=='primary') {
          const edit = button(f.tagged ? 'Change tag' : 'Tag this file',()=>openFileTags(name,[f.file_id]));
          edit.disabled=busy||submitting;detail.append(edit);
        }
        if(f.placement_note)detail.append(el('p',f.placement_note,'placement-note'));
        const a=el('a','Download original');a.className='small';a.href=endpoint(`download/uploads/${f.file_id}${f.path.match(/\.[^.]+$/)?.[0]||''}`);detail.append(a);
        item.append(detail);children.append(item);
      }
    }
    const actions=el('div',undefined,'tree-actions folder-upload-actions');
    const add=button('＋ Add files',()=>openUpload(false,name,''));
    add.disabled=busy||submitting||!!state.active_folder&&state.active_folder!==name;add.hidden=locked;actions.append(add);
    if(!locked) {
      const review=button('Complete ingestion',()=>openFolderSetup(name),'primary small');review.disabled=busy||submitting;actions.append(review);
    }
    if(locked || state.preparations?.[name]) {
      const restart=button('Restart ingestion',()=>restartFolder(name));restart.disabled=busy||submitting;actions.append(restart);
    }
    children.append(actions);d.append(children);$('tree').append(d);
  }
  if(!Object.keys(state.folders).length){const empty=el('div',undefined,'empty-tree');empty.append(el('strong','Start with your main data file'),el('span','We’ll create its folder. Then you can add more files together.'));$('tree').append(empty);}
}
/**
 * Show conflicting definitions with an instruction box per variable.
 *
 * When: Phase 4 final_codebook_creator supplies a codebook_review form.
 * Inputs: form: rows, field conflicts and source evidence.
 * Returns: Nothing; appends a review table and stores typed instructions in answers by
 *     variable_name.
 * Flow: Shows competing excerpts beside the affected cells; the answer handler sends the
 *     keyed instruction map to the waiting finalization function.
 */
function renderCodebookReview(form) {
  const columns = [
    ['variable_name','Variable'], ['% missing','% missing'], ['variable_type','Variable type'], ['variable_label','Label'],
    ['variable_description','Description'], ['other_information','Other information'],
    ['battery_root','Battery root'], ['battery_members','Battery members'], ['battery_summary','Battery summary'],
    ['categories','Categories'], ['theme','Theme'], ['subtheme','Subtheme'],
    ['original_variable_name','Original name'], ['table_names','Prepared tables'],
  ];
  const scroll = el('div', undefined, 'codebook-review-scroll');
  scroll.tabIndex = 0;
  scroll.setAttribute('aria-label', 'Codebook conflicts');
  const table = el('table', undefined, 'codebook-review');
  const head = el('thead'), headings = el('tr');
  for (const title of [...columns.map(([,title])=>title), 'Your instruction']) {
    const th = el('th', title); th.scope = 'col'; headings.append(th);
  }
  head.append(headings); table.append(head);
  const body = el('tbody');
  for (const row of form.rows) {
    const line = el('tr');
    for (const [key] of columns) {
      const value = Array.isArray(row[key]) ? row[key].join(', ') : row[key];
      const cell = el(key==='variable_name'?'th':'td', String(value ?? ''));
      if (key==='variable_name') cell.scope = 'row';
      for (const conflict of row.conflicts.filter(item=>item.field===key)) {
        cell.classList.add('definition-conflict');
        cell.append(el('p', conflict.reason));
        for (const claim of conflict.evidence) {
          const quote = el('div', undefined, 'conflict-quote');
          quote.append(el('small', claim.source), el('mark', claim.excerpt)); cell.append(quote);
        }
      }
      line.append(cell);
    }
    const instruction = el('td'), input = el('textarea');
    input.rows = 3; input.placeholder = 'Which wording should we use?';
    input.setAttribute('aria-label', `Instruction for ${row.variable_name}`);
    input.oninput = ()=>answers.set(row.variable_name,input.value);
    instruction.append(input); line.append(instruction); body.append(line);
  }
  table.append(body); scroll.append(table);
  $('question-form').append(el('p','Review the highlighted differences. Add an instruction per row or say “use best judgment”.'), scroll);
}
/**
 * Render the current ingestion question form and reply controls.
 *
 * When: The session becomes awaiting_user or its form ID changes.
 * Inputs: Reads question_form, pending state, answers and submission flags.
 * Returns: Nothing; updates fields, composer visibility and send labels.
 * Flow: Keeps drafts for the same form, handles choice/custom answers and delegates
 *     codebook_review forms to renderCodebookReview.
 */
function renderQuestions() {
  const waiting=state?.status==='awaiting_user', form=waiting?state.question_form:null;
  const codebookReview = form?.kind === 'codebook_review';
  const nextId=form?.id||'';
  document.querySelector('.chat-panel').classList.toggle('has-questions', !!form);
  if(nextId!==formId){
    formId=nextId;answers.clear();$('question-form').replaceChildren();
    if(codebookReview) renderCodebookReview(form);
    for(const [index,q] of (codebookReview ? [] : form?.questions||[]).entries()){
      const card=el('div',undefined,'question-card');card.append(el('span',`QUESTION ${index+1}`,'question-number'));
      const label=el('label',q.question);label.htmlFor=`answer-${q.id}`;card.append(label);
      if(q.help_text)card.append(el('p',q.help_text));
      const text=el('textarea');text.placeholder='Your answer';text.rows=1;text.id=`answer-${q.id}`;
      text.oninput=()=>answers.set(q.id,text.value);
      if(q.options?.length){
        const select=el('select');select.id=`answer-${q.id}`;text.id=`answer-other-${q.id}`;
        select.add(new Option('Choose an answer',''));for(const o of q.options)select.add(new Option(o,o));
        if(!q.options.some(o=>/don.t know|not sure/i.test(o)))select.add(new Option('I’m not sure','I’m not sure'));
        select.add(new Option('Write my own answer','__custom__'));text.hidden=true;
        select.onchange=()=>{text.hidden=select.value!=='__custom__';answers.set(q.id,select.value==='__custom__'?text.value:select.value);if(!text.hidden)text.focus();};card.append(select);
      }
      card.append(text);$('question-form').append(card);
    }
  }
  $('reply').disabled=!waiting||submitting;
  $('reply').placeholder=form?'Extra details (optional)':waiting?'Your answer…':'Extra details for your response…';
  $('reply').hidden=codebookReview||state?.status==='ready'||!!(state?.handoff_path&&['failed','stopped','interrupted'].includes(state.status));
  $('send').hidden=!waiting;$('send').disabled=!waiting||submitting;
  $('send').textContent=codebookReview?'Send review instructions':'Send reply ↑';
  $('composer-hint').textContent='Your next question will appear in chat. You can add extra details in your responses here.';
  $('pending').textContent=state?.status==='ready'?'Your materials are organised. Continue when you’re ready.':waiting&&!form?(state.pending||'Please reply to continue.'):' ';
  requestAnimationFrame(resizeIngestionInputs);
}

/** Size a one-line answer to its wrapped text, leaving one spare line after wrapping. */
function growIngestionInput(input) {
  if(!input.getClientRects().length) return;
  const style=getComputedStyle(input),line=parseFloat(style.lineHeight);
  const padding=parseFloat(style.paddingTop)+parseFloat(style.paddingBottom);
  const border=parseFloat(style.borderTopWidth)+parseFloat(style.borderBottomWidth);
  input.style.height='0px';
  const content=input.scrollHeight;
  input.style.height=`${content+border+(content>line+padding+2?line:0)}px`;
}

/** Recalculate visible ingestion answer boxes after changing pages or window width. */
function resizeIngestionInputs() {
  document.querySelectorAll('#reply, #question-form textarea, #setup-body textarea').forEach(growIngestionInput);
}
document.addEventListener('input',event=>{
  if(event.target.matches('#reply, #question-form textarea, #setup-body textarea')) growIngestionInput(event.target);
});
window.addEventListener('resize',resizeIngestionInputs);
/**
 * Show preparation reports, loader budget and saved artifact links.
 *
 * Inputs: Reads preparations, codebooks, log_directory and loader_budget.
 * Returns: Nothing; updates phase, budget and download sections.
 * Details: Uses cached signatures and converts saved artifact paths into project-relative
 *     download URLs.
 */
function renderReports(){
  const signature=JSON.stringify([state?.preparations,state?.codebooks,state?.log_directory,state?.loader_budget]);if(signature===phaseSignature)return;phaseSignature=signature;
  const budget=state?.loader_budget;$('budget').textContent=budget?`Session ${budget.session} · ${budget.remaining.toLocaleString()} of 250,000 tokens remaining`:'';
  $('phases').replaceChildren();for(const [folder,payload] of Object.entries(state?.preparations||{})){
    const group=el('div',undefined,'phase-folder');group.append(el('strong',folderLabel(folder)));
    for(const name of Object.keys(phaseNames)){const phase=payload.phases?.[name];if(!phase)continue;const d=disclosure(`phase-${folder}-${name}`,`${phase.completed?'✓':'○'} ${phaseNames[name]}`,'phase');d.append(el('p',phase.details));for(const key of ['observations','evidence_used','decisions_taken','key_findings'])if(phase[key]?.length){d.append(el('strong',key.replaceAll('_',' ')));const ul=el('ul');for(const item of phase[key])ul.append(el('li',item));d.append(ul);}group.append(d);}$('phases').append(group);
  }
  $('logs').replaceChildren();if(state?.log_directory){const run=state.log_directory.replaceAll('\\','/').split('/').pop();for(const [f,label] of [['timeline.md','Conversation log'],['timeline.jsonl','Detailed records']]){const a=el('a',label);a.href=endpoint(`download/runs/${run}/${f}`);$('logs').append(a);}}
  for(const [folder,book] of Object.entries(state?.codebooks||{})){
    const group=disclosure(`codebooks-${folder}`,`${folderLabel(folder)} · Codebook files`,'phase');
    const links=[[book.final,'Final codebook'],[book.final_unmatched,'Final unmatched codebook'],[book.soft_matched,'Soft-matched codebook'],[book.taxonomy,'Themes and subthemes'],[book.overview,'Planning overview'],[book.master,'Data variables'],[book.combined,'Combined source codebooks'],[book.unmatched,'Unmatched source rows'],[book.source_mapping,'Source mapping']];
    for(const source of Object.values(book.source_tables||{}))links.push([source.table,source.filename],[source.notes,`${source.filename} · General information`],[source.verification,`${source.filename} · Verification report`]);
    for(const [path,label] of links){if(!path)continue;const relative=path.replaceAll('\\','/').split(`/projects/${projectId}/`)[1];if(!relative)continue;const p=el('p'),a=el('a',label);a.href=endpoint(`download/${relative}`);p.append(a);group.append(p);}
    $('phases').append(group);
  }
}
/**
 * Persist editable context and clear drafts after success.
 *
 * Inputs: Reads current state and drafts; skips published folders.
 * Returns: Promise with no value; posts context/folders/files/dataset_contexts.
 * Details: Called before ingestion and project switching. Successful save invalidates the
 *     material-render cache; failures retain draft text.
 */
async function saveNotes(){
  const published = frozenFolders();
  if(!state||busy||!drafts.size)return;
  await api(endpoint('context'),{
    context:{description:drafts.get('project_description')??state.context.description??'',before_ingestion:drafts.get('before_ingestion')??state.context.before_ingestion??''},
    folders:Object.fromEntries(Object.entries(state.folders).filter(([name])=>!published.has(name)).map(([name,f])=>[name,{
      description:drafts.get(`folder:${name}`)??f.description??'', relationship:drafts.get(`relationship:${name}`)??f.relationship??''}])),
    files:Object.fromEntries(Object.values(state.files).filter(f=>!published.has(f.folder)).map(f=>[f.file_id,drafts.get(`file:${f.file_id}`)??f.note])),
    dataset_contexts:Object.fromEntries(Object.values(state.files).filter(f=>f.role!=='asset'&&!published.has(f.folder)).map(f=>[f.file_id,
      Object.fromEntries(datasetQuestions.map(([key])=>[key,drafts.get(`data:${f.file_id}:${key}`)??f.dataset_context?.[key]??'']))]))
  });
  drafts.clear();treeSignature='';
}
/**
 * Check the required dataset and relationship answers before ingestion.
 *
 * Inputs: Reads state and draft values, excluding published/ignored data.
 * Returns: Nothing on success; throws a readable missing-context error.
 * Details: Opens the relevant file panel for missing dataset fields and checks
 *     multi-file/folder descriptions before saveNotes/start.
 */
function validateContext(){
  const published = lockedFolders();
  const active = state.active_folder;
  const needsAnswers = name => (!active || name===active) && !published.has(name) && !state.folders[name]?.reuse_dataset;
  for(const f of Object.values(state.files).filter(f=>f.role!=='asset'&&!f.ignore&&needsAnswers(f.folder))) {
    for(const [key,label] of datasetQuestions)if(!(drafts.get(`data:${f.file_id}:${key}`)??f.dataset_context?.[key]??'').trim()) {
      expanded.set(`file-${f.file_id}`,true);treeSignature='';renderMaterials();throw Error(`Complete “${label}” for ${f.filename}.`);
    }
  }
  for(const [name,f] of Object.entries(state.folders)){
    if(!needsAnswers(name))continue;
    const count=Object.values(state.files).filter(x=>x.folder===name&&x.role!=='asset'&&!x.ignore).length;
    if(count>=2&&!(drafts.get(`relationship:${name}`)??f.relationship??'').trim())throw Error(`Explain how the data files in ${folderLabel(name)} fit together.`);
    if(name!==Object.keys(state.folders)[0]&&!(drafts.get(`folder:${name}`)??f.description??'').trim())throw Error(`Describe the folder ${folderLabel(name)}.`);
  }
}
/**
 * Open the appropriate project, folder or file deletion confirmation.
 *
 * Inputs: target: optional file/folder record; null means the project.
 * Returns: Nothing; stores deleteTarget and opens the dialog.
 * Details: The delete-form handler chooses the matching API route only after submission;
 *     server checks still decide what is deletable.
 */
function openDelete(target=null){
  deleteTarget=target;
  let title='Delete this project?';
  let description=`Permanently delete ${state.context.name}, its SQL schema and all tables/codebooks, registry entries, analysis chats, original uploads, descriptions, and saved outputs? This cannot be undone.`;
  if(target?.file_id){
    title='Delete this file?';
    const lastFile=!Object.values(state.files).some(file=>file.folder===target.folder && file.file_id!==target.file_id && file.embedded_for!==target.file_id);
    description=`Permanently delete ${target.filename}, its stored original and saved descriptions? ${lastFile?'This also removes its empty folder.':'Local preparation results will be reset.'}`;
  }else if(target){
    title='Delete this folder?';
    description=`Permanently delete ${folderLabel(target.folder)}, its original uploads, saved descriptions and working files? This cannot be undone.`;
  }
  $('delete-title').textContent=title;
  $('delete-description').textContent=description;
  $('delete-error').textContent='';$('delete-dialog').showModal();
}

/**
 * Open the create-project or rename dialog.
 *
 * Inputs: mode: new, folder or project rename; reads current labels.
 * Returns: Nothing; initializes the dialog and focuses its name field.
 * Details: The name-form handler uses nameMode and renameFolder to select the API
 *     operation.
 */
function openName(mode){
  nameMode=mode;$('name-title').textContent=mode==='new'?'New project':mode==='folder'?'Rename folder':'Rename project';
  $('name-description').textContent=mode==='new'?'Give this collection of related datasets a name.':'You can change the name at any time. Your files and conversation stay together.';
  $('project-name').value=mode==='new'?'':mode==='folder'?folderLabel(renameFolder):state.context.name;
  $('name-submit').textContent=mode==='new'?'Create project':'Save name';$('name-error').textContent='';$('name-dialog').showModal();$('project-name').focus();
}

/**
 * Open an upload form for a new dataset or added materials.
 *
 * Inputs: primary: new anchor file; folder: existing key; defaultRole: saved continuation
 *     preference.
 * Returns: Nothing; resets selection/form state and configures accepted formats.
 * Details: Requires an inactive selected project; renderSelectedFiles creates the per-file
 *     fields.
 */
function openUpload(primary,folder='',defaultRole='columns'){
  if(!state||busy)return;
  if(primary && state.active_folder && !state.folders[state.active_folder]?.setup_reviewed) throw Error('Finish the current folder’s form before starting another dataset.');
  uploadPrimary=primary;uploadFolder=folder;uploadDefault=defaultRole;selectedFiles=[];$('upload-form').reset();$('upload-error').textContent='';
  $('files').multiple=!primary;$('files').accept=primary?'.csv,.tsv,.sav,.zsav,.por,.dta,.dat,.xlsx,.xls,.txt':'.csv,.tsv,.sav,.zsav,.por,.dta,.dat,.xlsx,.xls,.txt,.md,.pdf,.dct,.dcf,.map';
  $('upload-title').textContent=primary?'Start a dataset':'Add materials';$('upload-destination').textContent=primary?'NEW DATASET':`ADDING TO ${folderLabel(folder)}`;
  $('upload-help').textContent=primary?'Upload the main data file first. Add its remaining files, then choose Complete ingestion to fill in the questionnaires.':'Upload one or several files. A tagging window will open for each file. The questionnaires come after all uploads. PDFs: up to 100 pages per file.';
  $('dataset-name-wrap').hidden=!primary;renderSelectedFiles();$('upload').showModal();
}
/**
 * Add local files to the pending upload selection.
 *
 * Inputs: files: browser File objects.
 * Returns: Nothing; updates selectedFiles and rerenders the cards.
 * Details: Allows one primary file. Content matching happens on the server after upload;
 *     the browser does not assume that matching names or sizes mean matching files.
 */
function addSelected(files){
  if(uploadPrimary&&files.length>1){$('upload-error').textContent='Start with one main data file. Add the rest after it has been uploaded.';return;}
  if(uploadPrimary)selectedFiles=[];
  for(const file of files) selectedFiles.push({file});
  $('upload-error').textContent='';renderSelectedFiles();
}
/**
 * Show the files queued for upload; additional files are tagged immediately afterward.
 *
 * Inputs: Reads selectedFiles and upload mode.
 * Returns: Nothing; updates cards, counts and submit availability.
 * Details: Only selection and removal happen here. Server-side matching uses file bytes.
 */
function renderSelectedFiles(){
  $('selected-files').replaceChildren();$('selected-count').textContent=selectedFiles.length?`${selectedFiles.length} file${selectedFiles.length===1?'':'s'} selected`:'No files selected';$('upload-submit').disabled=!selectedFiles.length||submitting;
  for(const [index,item] of selectedFiles.entries()){
    const card=el('div',undefined,'upload-item'),heading=el('div',undefined,'upload-item-heading');
    heading.append(el('span',item.file.name.split('.').pop().toUpperCase(),'file-icon'),el('strong',item.file.name),el('small',size(item.file.size),'muted'),button('×',()=>{selectedFiles.splice(index,1);renderSelectedFiles();},'delete-box'));
    heading.lastChild.setAttribute('aria-label',`Remove ${item.file.name}`);card.append(heading);
    $('selected-files').append(card);
  }
}

/**
 * Display exact-match provenance without changing the user's saved wording.
 * Inputs: matches: server-verified duplicate records. Returns: a collapsed details element.
 * Used beneath filenames and in the file-review modal; only filenames of companion files appear.
 */
function copyDetails(matches) {
  const details = el('details', undefined, 'copy-details');
  details.append(el('summary', 'File already exists — view source details', 'copy-label'));
  for (const match of matches) {
    const source = el('div', undefined, 'copy-source');
    source.append(el('strong', `${match.project_name} / ${match.folder_name || match.folder}`));
    source.append(el('p', `${match.filename || 'Matching file'} · ${Number(match.bytes || 0).toLocaleString()} bytes`, 'small'));
    const project = match.project_context || {}, folder = match.folder_context || {}, file = match.file_context || {};
    const notes = [
      ['Project description', project.description], ['Project instructions', project.before_ingestion],
      ['How project folders relate', project.folder_relationships], ['Folder description', folder.description],
      ['How these data files relate', folder.relationship], ['File notes', file.note],
      ...datasetQuestions.map(([key,label]) => [label,file.dataset_context?.[key]])
    ];
    const list = el('dl');
    for (const [label,value] of notes) if (typeof value === 'string' && value.length) {
      list.append(el('dt',label),el('dd',value,'preserved-text'));
    }
    source.append(list);
    if (match.other_files?.length) {
      source.append(el('strong','Other files in that folder'));
      const names = el('ul');
      for (const item of match.other_files) names.append(el('li',typeof item === 'string' ? item : item.filename));
      source.append(names);
    }
    details.append(source);
  }
  return details;
}

/** Tag only the newly uploaded supporting files, one at a time; ask no questionnaire yet. */
async function openFileTags(folder, fileIds, thenReview=false) {
  if (busy || submitting) return;
  if (frozenFolders().has(folder)) throw Error('Restart ingestion before changing this folder’s tags.');
  setupFolder=folder;setupMode='tags';reviewAfterTags=thenReview;
  questionnaire.clear();
  setupQueue=fileIds.filter(id=>state.files[id]&&!state.files[id].embedded_for&&state.files[id].role!=='primary');
  if(!setupQueue.length) return;
  setupIndex=0;reuseInfo=await api(endpoint(`reuse?folder=${encodeURIComponent(folder)}`));
  renderSetupStep();$('folder-setup').showModal();
}

/** Open all file questionnaires after the user finishes uploading this folder. */
async function openFolderSetup(folder, firstFile='') {
  if (busy || submitting) return;
  if (frozenFolders().has(folder)) throw Error('Restart ingestion before changing this folder’s submitted answers.');
  await saveNotes();
  const untagged=Object.values(state.files).filter(file=>file.folder===folder&&file.role!=='primary'&&!file.embedded_for&&!file.tagged);
  if(untagged.length) return openFileTags(folder,untagged.map(file=>file.file_id),true);
  setupFolder = folder;setupMode='questions';reviewAfterTags=false;
  setupQueue = Object.values(state.files).filter(file=>file.folder===folder&&!file.embedded_for)
    .sort((a,b)=>(a.role==='primary'?0:a.role==='asset'?2:1)-(b.role==='primary'?0:b.role==='asset'?2:1)).map(file=>file.file_id);
  questionnaire.clear();editedAnswers.clear();
  for(const id of setupQueue) {
    const file=state.files[id];
    questionnaire.set(id,{file_id:id,role:file.role==='primary'?'primary':file.role==='asset'?file.asset_kind||'related':file.continuation?.length===2?'both':file.continuation?.[0]||'columns',
      note:file.note||'',dataset_context:{...file.dataset_context},reuse_codebook:file.reuse_codebook===undefined?undefined:file.reuse_codebook||null});
  }
  const first=questionnaire.get(setupQueue[0]);
  for(const id of setupQueue.slice(1)) if(state.files[id].role!=='asset') {
    const context=questionnaire.get(id).dataset_context;
    for(const [key] of datasetQuestions) if(!context[key]) context[key]=first.dataset_context[key]||'';
  }
  setupIndex = firstFile ? Math.max(0,setupQueue.indexOf(firstFile)) : 0;
  reuseInfo = await api(endpoint(`reuse?folder=${encodeURIComponent(folder)}`));
  folderDraft={description:drafts.get(`folder:${folder}`)??state.folders[folder].description??'',
    relationship:drafts.get(`relationship:${folder}`)??state.folders[folder].relationship??'',
    reuse_dataset:state.folders[folder].reuse_dataset||null};
  if(!firstFile&&hasCopyChoices()) setupMode='copies';
  $('setup-error').textContent=''; renderSetupStep(); $('folder-setup').showModal();
}

/** Render upload tags separately from the later questionnaire and folder review. */
function renderSetupStep() {
  $('setup-body').replaceChildren(); $('setup-error').textContent='';
  $('folder-setup').querySelector('.upload-body').scrollTop=0;
  $('setup-back').hidden=setupMode==='copies'||setupMode==='tags'&&setupIndex===0||
    setupMode==='questions'&&setupIndex===0&&!hasCopyChoices();
  requestAnimationFrame(resizeIngestionInputs);
  if(setupMode==='copies') return renderCopyChoices();
  if(setupMode==='questions'&&setupIndex<setupQueue.length) return renderQuestionnaires();
  if (setupIndex >= setupQueue.length) return renderFolderReview();
  const file = state.files[setupQueue[setupIndex]];
  const role = file.role==='primary' ? 'primary' : !file.tagged ? '' : file.role==='asset'
    ? file.asset_kind || 'related' : file.continuation?.length===2 ? 'both' : file.continuation?.[0] || 'columns';
  if(!questionnaire.has(file.file_id)) questionnaire.set(file.file_id,{file_id:file.file_id,role,note:file.note||'',dataset_context:{...file.dataset_context},reuse_codebook:file.reuse_codebook||null});
  setupDraft = questionnaire.get(file.file_id);
  $('setup-progress').textContent=`${folderLabel(setupFolder)} · Tag file ${setupIndex+1} of ${setupQueue.length}`;
  $('setup-title').textContent=file.filename;
  $('setup-submit').textContent=setupIndex+1<setupQueue.length ? `Save and next: ${state.files[setupQueue[setupIndex+1]].filename}` : 'Save tag';
  const body=$('setup-body');
  body.append(el('p',`${file.bytes.toLocaleString()} bytes`,'muted small'));
  const matches=reuseInfo.files?.[file.file_id]||file.duplicates||[];
  if(matches.length) {
    body.append(el('p','Exact copy already exists','copy-label'),copyDetails(matches));
  }
  if(role!=='primary') {
    const label=el('label','What kind of file is this?'),select=el('select');select.id='setup-role';label.htmlFor=select.id;select.required=true;
    select.add(new Option('Choose a file type',''));
    select.add(new Option('Codebook','codebook'));
    if(!['pdf','md','dct','dcf','map'].includes(file.format)) {
      const group=el('optgroup');group.label='Data';
      const heading=new Option('　Data continuation','');heading.disabled=true;group.append(heading);
      for(const [value,title] of [['rows','Add rows'],['columns','Add columns'],['both','Add rows and columns']]) {
        group.append(new Option(`　　${title}`,value));
      }
      select.append(group);
    }
    select.add(new Option('Report or presentation','report'));select.add(new Option('Other related material','related'));select.value=setupDraft.role;
    select.onchange=()=>{setupDraft.role=select.value;};
    body.append(label,select);
  } else body.append(el('p','Primary dataset','muted small'));
  body.append(el('p','Descriptions and dataset questions come after all files are uploaded.','muted small'));
}

/** Show data forms together, then one supporting file per page of the same modal.
 * The main file stays first and fills unchanged answers in the other data forms live.
 */
function renderQuestionnaires() {
  const current=state.files[setupQueue[setupIndex]],showData=current.role!=='asset';
  const dataIds=setupQueue.filter(id=>['primary','continuation'].includes(state.files[id].role));
  const supporting=setupQueue.filter(id=>state.files[id].role==='asset');
  const nextId=showData?supporting[0]:setupQueue[setupIndex+1];
  $('setup-progress').textContent=`${folderLabel(setupFolder)} · ${showData?'Data files':`Supporting file ${supporting.indexOf(current.file_id)+1} of ${supporting.length}`}`;
  $('setup-title').textContent=showData?'Describe the data files':current.filename;
  $('setup-submit').textContent=nextId?`Save and next: ${state.files[nextId].filename}`:'Save and review folder →';
  const body=$('setup-body');
  if(showData) body.append(el('p','The main data file is first. Its answers fill the other data forms as you type. Edit an answer below if that file needs different details. Supporting files come next, one at a time.','muted small'));
  const firstId=dataIds[0];
  for(const [index,id] of (showData?dataIds:[current.file_id]).entries()) {
    const file=state.files[id],draft=questionnaire.get(id);
    const card=el('section',undefined,'questionnaire-file');card.id=`questionnaire-${id}`;
    card.dataset.tone=String(index%2);
    card.classList.toggle('current-file',id===setupQueue[setupIndex]);
    card.append(el('h3',file.filename),el('p',file.role==='primary'?'Main data file':file.role==='asset'?'Supporting file':'Data continuation','muted small'));
    const matches=reuseInfo.files?.[id]||file.duplicates||[];
    if(matches.length) card.append(copyDetails(matches));
    if(file.role!=='asset') {
      const fields=el('div',undefined,'dataset-questions');
      for(const [key,title,help] of datasetQuestions) {
        const field=el('div',undefined,'dataset-question'),label=el('label',title),input=el('textarea');
        input.id=`question-${id}-${key}`;label.htmlFor=input.id;input.rows=1;
        input.value=draft.dataset_context[key]||'';
        input.oninput=()=>{
          const previous=draft.dataset_context[key]||'';
          draft.dataset_context[key]=input.value;
          if(id!==firstId) editedAnswers.add(`${id}:${key}`);
          if(id===firstId) for(const otherId of setupQueue) {
            if(otherId===id||state.files[otherId].role==='asset'||editedAnswers.has(`${otherId}:${key}`)) continue;
            const other=questionnaire.get(otherId).dataset_context;
            if(!other[key]||other[key]===previous) {
              other[key]=input.value;const otherInput=$(`question-${otherId}-${key}`);otherInput.value=input.value;growIngestionInput(otherInput);
            }
          }
        };
        const hint=el('p',help,'field-help');hint.id=`${input.id}-help`;input.setAttribute('aria-describedby',hint.id);
        field.append(label,hint,input);fields.append(field);
      }
      card.append(fields);
    } else {
      const fields=el('div');card.append(fields);renderFileFields(fields,file,matches,draft);
    }
    body.append(card);
  }
  setupDraft=questionnaire.get(setupQueue[setupIndex]);
}

/** Pause a data-continuation tag for a five-second reminder, then allow dismissal.
 * Called before saving a data tag. Closing continues Save/next; Back to tagging cancels it.
 * The timer belongs only to this open reminder and creates no saved project state.
 */
function showContinuationWarning() {
  const dialog=$('continuation-warning'),close=$('continuation-warning-close');
  $('continuation-warning-text').textContent=continuationWarning;
  close.disabled=true;close.setAttribute('aria-label','Please read this reminder');
  close.replaceChildren(el('span',undefined,'warning-loader'));
  dialog.returnValue='';
  return new Promise(resolve=>{
    const timer=setTimeout(()=>{
      close.textContent='×';close.disabled=false;close.setAttribute('aria-label','Close reminder and continue');
    },5000);
    close.onclick=()=>dialog.close('continue');
    $('continuation-warning-back').onclick=()=>dialog.close('back');
    dialog.oncancel=event=>{event.preventDefault();if(!close.disabled)dialog.close('continue');};
    dialog.addEventListener('close',()=>{clearTimeout(timer);resolve(dialog.returnValue==='continue');},{once:true});
    dialog.showModal();
  });
}

/** Offer a copy page only for matching data or reusable, previously ingested codebooks. */
function hasCopyChoices() {
  return setupQueue.some(id=>state.files[id].role!=='asset'&&reuseInfo.files?.[id]?.length ||
    state.files[id].asset_kind==='codebook'&&reuseInfo.codebook_matches?.[id]?.length);
}

/** Resolve copy choices before the questionnaires; selections fill the existing draft answers. */
function renderCopyChoices() {
  $('setup-progress').textContent=`${folderLabel(setupFolder)} · Exact copies`;
  $('setup-title').textContent='Copies of these files already exist';
  $('setup-submit').textContent='Continue to file questions →';
  const body=$('setup-body'),sources=reuseInfo.dataset_matches||[];
  for(const id of setupQueue.filter(id=>state.files[id].role!=='asset')) {
    const matches=reuseInfo.files?.[id]||[];
    if(matches.length) {
      const card=el('div',undefined,'review-file');
      card.append(el('strong',state.files[id].filename),copyDetails(matches));body.append(card);
    }
  }
  if(sources.length) {
    const choices=el('div',undefined,'reuse-choice');
    const label=el('label','This entire data set matches another folder. Reuse its prepared data?');
    const select=el('select');select.id='setup-dataset-reuse';label.htmlFor=select.id;
    select.add(new Option('Ingest this separately',''));
    sources.forEach((source,index)=>select.add(new Option(`Yes, copy: ${source.project_name} / ${source.folder_name||source.folder}`,String(index))));
    const selected=sources.findIndex(source=>source.project_id===folderDraft.reuse_dataset?.project_id&&source.folder===folderDraft.reuse_dataset?.folder);
    select.value=selected<0?'':String(selected);
    select.onchange=()=>{
      const source=select.value===''?null:sources[Number(select.value)];
      folderDraft.reuse_dataset=source?{project_id:source.project_id,folder:source.folder}:null;
      for(const key of ['description','relationship']) folderDraft[key]=source?.folder_context?.[key]??state.folders[setupFolder][key]??'';
      for(const id of setupQueue.filter(id=>state.files[id].role!=='asset')) {
        const match=source&&(reuseInfo.files?.[id]||[]).find(item=>item.project_id===source.project_id&&item.file_id===source.files[id]);
        const draft=questionnaire.get(id),file=state.files[id];
        draft.dataset_context={...(match?match.file_context?.dataset_context:file.dataset_context)};
        draft.note=(match?match.file_context?.note:file.note)||'';
      }
    };
    choices.append(label,select);body.append(choices);
  }
  for(const id of setupQueue.filter(id=>state.files[id].asset_kind==='codebook'&&reuseInfo.codebook_matches?.[id]?.length)) {
    const card=el('div',undefined,'review-file');card.append(el('strong',state.files[id].filename));
    renderCodebookChoice(card,state.files[id],questionnaire.get(id));body.append(card);
  }
}

/** Choose an existing codebook only on the exact-copy page; keep the choice in its file draft. */
function renderCodebookChoice(fields,file,draft) {
      const sources=reuseInfo.codebook_matches?.[file.file_id]||[];
      if(sources.length) {
        const label=el('label','Use a completed codebook?'),select=el('select');select.id=`setup-codebook-reuse-${file.file_id}`;label.htmlFor=select.id;
        select.add(new Option('Ingest this separately',''));
        sources.forEach((source,index)=>select.add(new Option(`Yes, reuse: ${source.project_name} / ${source.folder_name||source.folder}`,String(index))));
        const previous=sources.findIndex(source=>source.project_id===draft.reuse_codebook?.project_id&&source.file_id===draft.reuse_codebook?.file_id);
        select.value=previous<0?(draft.reuse_codebook===undefined?'0':''):String(previous);
        if(select.value!=='') {
          const source=sources[Number(select.value)];
          draft.reuse_codebook={project_id:source.project_id,folder:source.folder,file_id:source.file_id};
          if(previous<0) draft.note=source.file_context?.note||'';
        }
        select.onchange=()=>{
          const source=select.value===''?null:sources[Number(select.value)];
          draft.reuse_codebook=source?{project_id:source.project_id,folder:source.folder,file_id:source.file_id}:null;
          if(source) draft.note=source.file_context?.note||'';
        };
        fields.append(label,select,el('p','Reuse is available because the bytes match a file previously ingested as a codebook.','muted small'));
      }
}

/** Ask for the current supporting file's description on its own questionnaire page. */
function renderFileFields(fields,file,matches,draft) {
    fields.replaceChildren();
    const label=el('label','What does this file explain?'),input=el('textarea');input.id=`setup-file-note-${file.file_id}`;label.htmlFor=input.id;
    input.rows=1;input.value=draft.note;input.oninput=()=>draft.note=input.value;fields.append(label,input);
}

/** Review the saved answers before closing this folder's form; starting agents is separate. */
function renderFolderReview() {
  $('setup-progress').textContent=`${folderLabel(setupFolder)} · Folder review`;
  $('setup-title').textContent='Review this dataset';$('setup-submit').textContent='Save folder';
  const body=$('setup-body');
  body.append(el('p','Save this form to add another folder, or choose Ingest materials when all your folders are ready.','muted'));
  const files=setupQueue.map(id=>state.files[id]),data=files.filter(file=>file.role!=='asset'&&!file.ignore);
  for(const file of data) {
    const card=el('div',undefined,'review-file'),head=el('div',undefined,'upload-item-heading');
    head.append(el('strong',file.filename),button('Edit details',()=>{setupIndex=setupQueue.indexOf(file.file_id);renderSetupStep();},'quiet small'));
    card.append(head,el('p',`${file.bytes.toLocaleString()} bytes`,'muted small'));
    const matches=reuseInfo.files?.[file.file_id]||[];
    if(matches.length) card.append(copyDetails(matches));
    const panel=el('details',undefined,'copy-details'),list=el('dl');
    panel.append(el('summary','File answers'));
    for(const [key,title] of datasetQuestions) list.append(el('dt',title),el('dd',questionnaire.get(file.file_id).dataset_context[key]||'Answer needed before ingestion','preserved-text'));
    panel.append(list);card.append(panel);
    body.append(card);
  }
  setupDraft=folderDraft;
  if(folderDraft.reuse_dataset) body.append(el('p','Prepared data from your selected copy will be reused.','copy-label'));
  for(const [key,title] of [['description','Describe this folder and how it relates to the project'],['relationship','How do these data files fit together?']]) {
    if(key==='relationship'&&data.length<2) continue;
    const label=el('label',title),input=el('textarea');input.id=`setup-folder-${key}`;label.htmlFor=input.id;
    input.rows=1;input.value=setupDraft[key];input.oninput=()=>{setupDraft[key]=input.value;drafts.set(`${key==='description'?'folder':'relationship'}:${setupFolder}`,input.value);};body.append(label,input);
  }
  const books=files.filter(file=>file.asset_kind==='codebook');
  if(books.length) {
    body.append(el('h3','Codebooks'));
    const list=el('ul');for(const file of books) {
      const item=el('li',`${file.filename} — ${file.reuse_codebook?'reuse completed codebook':'ingest separately'} `);
      item.append(button('Edit',()=>{setupIndex=setupQueue.indexOf(file.file_id);renderSetupStep();},'quiet small'));list.append(item);
    }body.append(list);
  }
}

/** Reset this folder and later results only after the user confirms the restart. */
async function restartFolder(folder) {
  if(!window.confirm(`Restart ingestion from ${folderLabel(folder)}? This folder and any later folders will lose their prepared/published results. Original uploads and your answers stay saved. You can edit them before starting again.`)) return;
  await perform(async()=>{await api(endpoint('restart-ingestion'),{folder,confirmation:'restart'});activeView='ingest';treeSignature='';});
}

/** Save the data page together, then each supporting file; the server rechecks reuse. */
$('setup-form').onsubmit=async event=>{
  event.preventDefault();if(submitting)return;
  if(setupMode==='copies') {setupMode='questions';setupIndex=0;renderSetupStep();return;}
  submitting=true;$('setup-submit').disabled=true;$('setup-error').textContent='';
  let continueReview=false;
  try {
    if(setupIndex<setupQueue.length) {
      const data=['primary','rows','columns','both'].includes(setupDraft.role);
      if(!setupDraft.role) throw Error('Choose a file type.');
      if(setupMode==='tags'&&data&&!await showContinuationWarning()) return;
      const payload={
        file_id:setupDraft.file_id,role:setupDraft.role==='primary'?'primary':data?'continuation':'asset',
        asset_kind:data?'':setupDraft.role,
        continuation:setupDraft.role==='both'?['rows','columns']:['rows','columns'].includes(setupDraft.role)?[setupDraft.role]:[]
      };
      if(setupMode==='questions') {
        // Save the shared answers together, including fields already filled for later files.
        await api(endpoint('context'),{context:{},
          dataset_contexts:Object.fromEntries([...questionnaire].filter(([id])=>state.files[id].role!=='asset').map(([id,draft])=>[id,draft.dataset_context])),
          files:Object.fromEntries([...questionnaire].map(([id,draft])=>[id,draft.note]))});
        Object.assign(payload,{note:setupDraft.note,dataset_context:setupDraft.dataset_context,
          reuse_codebook:setupDraft.role==='codebook'?setupDraft.reuse_codebook:null});
      }
      await api(endpoint('file-details'),payload);
      state=await api(`/api/projects/${projectId}`);
      if(setupMode==='questions'&&data) {
        // All data answers were saved together; supporting files each get their own page.
        const next=setupQueue.findIndex(id=>state.files[id].role==='asset');
        setupIndex=next<0?setupQueue.length:next;
      } else setupIndex++;
      if(setupMode==='tags'&&setupIndex===setupQueue.length) {
        $('folder-setup').close();continueReview=reviewAfterTags;
      } else {
        reuseInfo=await api(endpoint(`reuse?folder=${encodeURIComponent(setupFolder)}`));renderSetupStep();
      }
    } else {
      await saveNotes();
      const result=await api(endpoint('folder-setup'),{folder:setupFolder,...setupDraft});
      state=result.state||await api(`/api/projects/${projectId}`);
      drafts.delete(`folder:${setupFolder}`);drafts.delete(`relationship:${setupFolder}`);
      $('folder-setup').close();
    }
  } catch(error) {$('setup-error').textContent=error.message;}
  finally {submitting=false;$('setup-submit').disabled=false;treeSignature='';await refresh().catch(fail);render();}
  if(continueReview) await openFolderSetup(setupFolder).catch(fail);
};

/** Revisit the preceding form page without discarding unsaved answers or copy choices. */
$('setup-back').onclick=()=>{
  if(submitting) return;
  if(setupMode==='tags') setupIndex=Math.max(0,setupIndex-1);
  else if(setupIndex===0&&hasCopyChoices()) setupMode='copies';
  else {
    const previous=setupIndex-1;
    setupIndex=previous>=0&&state.files[setupQueue[previous]].role==='asset'?previous:0;
  }
  renderSetupStep();
};

/**
 * Run a UI write action with shared busy and error handling.
 *
 * Inputs: action: async callback; errorTarget: optional inline error element.
 * Returns: Promise with no value; refreshes state when the action finishes.
 * Details: Blocks duplicate submissions, shows errors in the chosen location, and always
 *     restores controls through render.
 */
async function perform(action,errorTarget=null){if(submitting)return;submitting=true;clearNotice();render();try{await action();}catch(e){if(errorTarget)errorTarget.textContent=e.message;else fail(e);}finally{submitting=false;await refresh().catch(fail);render();}}
/**
 * Offer a reminder when useful supporting material is missing.
 *
 * Inputs: Reads active asset roles and ingestion findings.
 * Returns: True if the reminder dialog opens, otherwise false.
 * Details: Used before beginIngestion; checks codebook and report availability without
 *     making either mandatory.
 */
function suggestSupportingMaterials() {
  const files = Object.values(state.files).filter(file => file.role === 'asset' && !file.ignore);
  const items = $('missing-materials');
  items.replaceChildren();
  if (!files.some(file => file.asset_kind === 'codebook' || state.findings?.[file.file_id]?.classification === 'data book')) {
    items.append(el('li', 'Add a codebook if available. It helps us read variable names, categories and missing-value codes correctly.'));
  }
  if (!files.some(file => ['report','presentation'].includes(file.asset_kind) ||
      ['report','presentation'].includes(state.findings?.[file.file_id]?.classification))) {
    items.append(el('li', 'Add a report or presentation if available. It helps us understand who the data covers and how it was collected.'));
  }
  if (!items.children.length) return false;
  $('materials-check').showModal();
  return true;
}
/**
 * Validate and save context, then start material review.
 *
 * Inputs: Uses the selected project and draft state; no arguments.
 * Returns: Promise from perform.
 * Details: Calls validateContext, saveNotes and the start endpoint in order, routing
 *     errors to the ingestion panel.
 */
function beginIngestion() {
  return perform(async()=>{validateContext();await saveNotes();await api(endpoint('start'),{});}, $('ingest-error'));
}
$('new').onclick=()=>openName('new');$('rename').onclick=()=>openName('rename');
/**
 * Submit project creation or a display-name change.
 *
 * When: The name dialog is submitted.
 * Inputs: event: form submission; reads nameMode, input text and optional renameFolder.
 * Returns: Promise with no value; updates selection/list or displays an inline error.
 * Flow: Prevents browser submission, calls the appropriate API and refreshes the selected
 *     project.
 */
$('name-form').onsubmit=async event=>{event.preventDefault();$('name-submit').disabled=true;try{const name=$('project-name').value.trim();if(nameMode==='new'){const p=await api('/api/projects',{name});projectId=p.id;state=p;resetView();render();}else if(nameMode==='folder'){await api(endpoint('rename-folder'),{name,folder:renameFolder});treeSignature='';}else{await api(endpoint('rename'),{name});}$('name-dialog').close();await listProjects();await refresh();}catch(e){$('name-error').textContent=e.message;}finally{$('name-submit').disabled=false;}};
for(const b of document.querySelectorAll('.close-dialog'))b.onclick=()=>$(b.dataset.dialog).close();
/**
 * Switch projects after saving pending editable notes.
 *
 * When: The project selector changes.
 * Inputs: Reads selector value, drafts and current busy state.
 * Returns: Promise with no value; replaces project state or restores selection on save
 *     failure.
 * Flow: Saves notes first, then resetView clears old UI state before refresh loads the
 *     next project.
 */
$('projects').onchange=async()=>{const next=$('projects').value;if(drafts.size&&!busy){try{await saveNotes();}catch(e){$('projects').value=projectId;fail(e);return;}}projectId=next;state=null;resetView();clearNotice();render();await refresh().catch(fail);};
$('dataset').onclick=()=>openUpload(true);
$('view-analyse').onclick=()=>{activeView='analyse';render();};
$('view-ingest').onclick=()=>{activeView='ingest';render();};
$('before-note').oninput=()=>{const value=$('before-note').value;if(value===(state?.context.before_ingestion??'')) drafts.delete('before_ingestion');else drafts.set('before_ingestion',value);$('save-state').textContent=drafts.size?'Unsaved notes':'Notes saved with this project';renderRetry();};
$('delete-project').onclick=()=>openDelete();
/**
 * Submit the confirmed project, folder or file removal.
 *
 * When: The delete dialog is submitted.
 * Inputs: event: form submission; reads deleteTarget and selected project.
 * Returns: Promise with no value; refreshes remaining state or clears a deleted project.
 * Flow: Sends confirmation=delete to the selected route, keeping server errors in the
 *     dialog and restoring the submit button.
 */
$('delete-form').onsubmit=async event=>{
  event.preventDefault();$('delete-submit').disabled=true;
  try{
    const removal={confirmation:'delete'};
    let route='delete';
    if(deleteTarget?.file_id){route='delete-file';removal.file_id=deleteTarget.file_id;}
    else if(deleteTarget){route='delete-folder';removal.folder=deleteTarget.folder;}
    await api(endpoint(route),removal);
    $('delete-dialog').close();
    if(deleteTarget){drafts.clear();treeSignature='';await refresh();}else{projectId='';state=null;resetView();await listProjects();render();}
  }catch(e){$('delete-error').textContent=e.message;}finally{$('delete-submit').disabled=false;}
};
$('files').onchange=()=>{addSelected([...$('files').files]);$('files').value='';};
$('dropzone').ondragover=event=>{event.preventDefault();$('dropzone').classList.add('dragover');};$('dropzone').ondragleave=()=>$('dropzone').classList.remove('dragover');$('dropzone').ondrop=event=>{event.preventDefault();$('dropzone').classList.remove('dragover');addSelected([...event.dataTransfer.files]);};
/**
 * Upload originals before asking for their roles and descriptions.
 *
 * When: The upload dialog is submitted.
 * Inputs: event: form submission; reads selectedFiles, upload mode/folder and draft notes.
 * Returns: Promise with no value; closes and refreshes on success or keeps an inline
 *     error.
 * Flow: Save files without questions. Additional uploads immediately enter the tag-only
 *     sequence; Complete ingestion opens the questionnaires after the folder is assembled.
 */
$('upload-form').onsubmit=async event=>{
  event.preventDefault();if(submitting)return;submitting=true;$('upload-submit').disabled=true;
  let tagIds=[];
  try {
    if(drafts.size)await saveNotes();
    const form=new FormData();form.set('role',uploadPrimary?'primary':'mixed');
    form.set('folder',uploadPrimary?$('dataset-name').value.trim():uploadFolder);
    for(const item of selectedFiles)form.append('files',item.file);
    const result=await api(endpoint('upload'),form);if(!uploadPrimary) tagIds=result.file_ids||[];
    $('upload').close();treeSignature='';state=await api(`/api/projects/${projectId}`);render();
  }catch(e){$('upload-error').textContent=e.message;}
  finally{submitting=false;$('upload-submit').disabled=!selectedFiles.length;render();}
  if(tagIds.length) await openFileTags(uploadFolder,tagIds).catch(fail);
};
$('save').onclick=()=>perform(saveNotes);
/**
 * Check readiness before starting ingestion.
 *
 * When: The Ingest materials button is clicked.
 * Inputs: Reads required context and supporting-material availability.
 * Returns: Nothing or the beginIngestion promise.
 * Flow: Shows context errors immediately, otherwise offers the optional materials reminder
 *     before calling beginIngestion.
 */
$('ingest').onclick=()=>{
  const error = $('ingest-error');
  error.textContent = '';
  if(state.active_folder && !frozenFolders().has(state.active_folder)) return openFolderSetup(state.active_folder).catch(e=>error.textContent=e.message);
  try { validateContext(); } catch(e) { error.textContent = e.message; return; }
  if (!suggestSupportingMaterials()) return beginIngestion();
};
$('add-ingestion-details').onclick=()=>{$('materials-check').close();$('before-note').focus();};
$('continue-ingestion').onclick=()=>{$('materials-check').close();return beginIngestion();};
/**
 * Continue from a review pause or retry saved preparation.
 *
 * When: The Prepare data button is clicked.
 * Inputs: Reads current project status.
 * Returns: Promise from perform.
 * Flow: A ready live session receives an answer; a stopped/failed session starts with
 *     prepare_only so phase-1 review is reused.
 */
$('prepare').onclick=()=>perform(()=>api(endpoint('answer'),{text:'Prepare the data.'}));
$('retry-ingestion').onclick=()=>perform(()=>api(endpoint('start'),{retry:true}),$('ingest-error'));
$('publish').onclick=()=>{$('ingest-error').textContent='';return perform(()=>api(endpoint('publish'),{}),$('ingest-error'));};
/**
 * Send a plain reply or the active structured answers.
 *
 * When: The ingestion Send button is clicked.
 * Inputs: Reads reply text, formId and answers keyed by question/variable ID.
 * Returns: Promise from perform; clears text after a successful answer request.
 * Flow: Requires all displayed fields, attaches the current form ID and sends the payload
 *     expected by the waiting Session.answer.
 */
$('send').onclick=()=>perform(async()=>{let body={text:$('reply').value};if(formId){const fields=state.question_form.questions;for(const q of fields)if(!answers.get(q.id)?.trim())throw Error('Answer each question, or write “I’m not sure”.');body={form_id:formId,answers:Object.fromEntries(answers),text:$('reply').value};}else if(!body.text.trim())throw Error('Write a short reply before sending.');await api(endpoint('answer'),body);$('reply').value='';});
$('reply').onkeydown=event=>{if((event.ctrlKey||event.metaKey)&&event.key==='Enter'&&!$('send').disabled)$('send').click();};
$('stop').onclick=()=>perform(async()=>{
  await api(endpoint('stop'),{});
  state.status='stopping'; // Show the waiting ring immediately, including between polls.
});
/**
 * Initialize the browser session and start project polling.
 *
 * When: app.js finishes loading.
 * Inputs: No arguments; fetches the current request token.
 * Returns: Promise with no value; renders projects and installs the polling timer.
 * Flow: Loads status before listProjects so subsequent writes have X-V5-Token; startup
 *     errors go to fail.
 */
(async()=>{try{token=(await api('/api/status')).token;await listProjects();render();setInterval(()=>refresh().catch(fail),2000);setInterval(()=>liveMetrics.forEach(update=>update()),1000);}catch(e){fail(e);}})();
