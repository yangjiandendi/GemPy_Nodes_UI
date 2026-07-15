const state = {
  nodeTypes: [],
  nodeTypeById: {},
  nodes: [],
  edges: [],
  uploads: [],
  examples: [],
  selectedNodeId: null,
  selectedNodeIds: [],
  workflowTemplates: [],
  pendingConnection: null,
  drag: null,
  pan: null,
  nodeCounter: 1,
  lastExecution: null,
  lastRunScope: null,
  nodeResults: {},
  nodeStatuses: {},
  runLog: [],
  lastManifest: null,
  contextMenu: null,
  executionPollTimer: null,
  activeRunNodeIds: [],
  runInProgress: false,
  activeProgressRunId: null,
  progressPollStartedAt: 0,
};

const el = (id) => document.getElementById(id);
const canvas = el('canvas');
const svg = el('edgeSvg');
const hint = el('connectionHint');

const STRUCTURAL_RELATIONS = ['FAULT', 'ERODE', 'ONLAP', 'BASEMENT'];
const COMMON_ELEMENTS = [
  'F1', 'F2', 'F3', 'F4', 'f1', 'f2', 'f3', 'f4',
  'Quaternary', 'Buntsandstein', 'Zechstein', 'PraePerm', 'Praeperm', 'GG', 'Rhyolith', 'Rotliegend',
  'Granit', 'Granodiorit', 'Flasergranitoid', 'Diorit, Gabbro', 'Orthogneis', 'Amphibolit', 'Metasedimente',
  'CrystallineBasement', 'Crystalline Basement', 'base'
];

function uuid(prefix='id') {
  return `${prefix}_${Math.random().toString(36).slice(2, 9)}_${Date.now().toString(36).slice(2)}`;
}

async function api(url, options={}) {
  const res = await fetch(url, options);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}: ${await res.text()}`);
  return await res.json();
}


function deepClone(value) {
  try {
    return structuredClone(value);
  } catch {
    return JSON.parse(JSON.stringify(value ?? null));
  }
}

function nextNodeId(type) {
  let id = `${type}_${state.nodeCounter++}`;
  const used = new Set(state.nodes.map(n => n.id));
  while (used.has(id)) {
    id = `${type}_${state.nodeCounter++}`;
  }
  return id;
}

function selectedNodeIdSet() {
  return new Set(state.selectedNodeIds || []);
}

function normalizeSelectedNodes() {
  const existing = new Set(state.nodes.map(n => n.id));
  state.selectedNodeIds = (state.selectedNodeIds || []).filter(id => existing.has(id));
  if (state.selectedNodeId && !existing.has(state.selectedNodeId)) state.selectedNodeId = state.selectedNodeIds[0] || null;
  if (!state.selectedNodeId && state.selectedNodeIds.length) state.selectedNodeId = state.selectedNodeIds[state.selectedNodeIds.length - 1];
}

function showHint(message, ms=2200) {
  hint.textContent = message;
  hint.classList.remove('hidden');
  setTimeout(() => hint.classList.add('hidden'), ms);
}

function closeContextMenu() {
  const existing = document.querySelector('.node-context-menu');
  if (existing) existing.remove();
  state.contextMenu = null;
}

function showNodeContextMenu(ev, nodeId) {
  ev.preventDefault();
  ev.stopPropagation();
  closeContextMenu();

  if (!(state.selectedNodeIds || []).includes(nodeId)) {
    selectNode(nodeId, { rerenderNodes: false });
  } else {
    state.selectedNodeId = nodeId;
  }
  normalizeSelectedNodes();

  const selectedCount = (state.selectedNodeIds || []).length;
  const menu = document.createElement('div');
  menu.className = 'node-context-menu';
  menu.innerHTML = `
    <button type="button" data-action="duplicate">Duplicate node</button>
    <button type="button" data-action="select-upstream">Select upstream workflow</button>
    <button type="button" data-action="duplicate-selected" ${selectedCount > 1 ? '' : 'disabled'}>Duplicate selected workflow</button>
    <button type="button" data-action="save-workflow" ${selectedCount > 1 ? '' : 'disabled'}>Save selected as workflow</button>
  `;
  document.body.appendChild(menu);

  const x = Math.min(ev.clientX, window.innerWidth - 230);
  const y = Math.min(ev.clientY, window.innerHeight - 150);
  menu.style.left = `${Math.max(8, x)}px`;
  menu.style.top = `${Math.max(8, y)}px`;

  menu.querySelector('[data-action="duplicate"]').onclick = (clickEv) => {
    clickEv.preventDefault();
    clickEv.stopPropagation();
    duplicateNode(nodeId);
    closeContextMenu();
  };
  menu.querySelector('[data-action="select-upstream"]').onclick = (clickEv) => {
    clickEv.preventDefault();
    clickEv.stopPropagation();
    selectUpstreamWorkflow(nodeId);
    closeContextMenu();
  };
  menu.querySelector('[data-action="duplicate-selected"]').onclick = (clickEv) => {
    clickEv.preventDefault();
    clickEv.stopPropagation();
    duplicateSelectedWorkflow();
    closeContextMenu();
  };
  menu.querySelector('[data-action="save-workflow"]').onclick = (clickEv) => {
    clickEv.preventDefault();
    clickEv.stopPropagation();
    saveSelectedAsWorkflow();
    closeContextMenu();
  };

  state.contextMenu = { nodeId };
}

function duplicateNode(nodeId) {
  const original = state.nodes.find(n => n.id === nodeId);
  if (!original) return null;

  const id = nextNodeId(original.type);
  const pos = original.position || { x: 80, y: 80 };
  const copy = {
    ...deepClone(original),
    id,
    params: deepClone(original.params || {}),
    position: {
      x: Number(pos.x || 0) + 36,
      y: Number(pos.y || 0) + 36,
    },
  };

  state.nodes.push(copy);
  delete state.nodeResults[id];
  delete state.nodeStatuses[id];

  selectNode(id, { rerenderNodes: true });
  showHint(`Duplicated ${original.id} → ${id}`, 1800);
  renderAll();
  return copy;
}

function workflowStorageKey() {
  return 'gempy_node_editor_custom_workflows_v1';
}

function loadWorkflowTemplates() {
  try {
    const raw = localStorage.getItem(workflowStorageKey());
    const arr = raw ? JSON.parse(raw) : [];
    return Array.isArray(arr) ? arr : [];
  } catch {
    return [];
  }
}

function persistWorkflowTemplates() {
  try {
    localStorage.setItem(workflowStorageKey(), JSON.stringify(state.workflowTemplates || []));
  } catch (err) {
    console.warn('Could not persist workflow templates:', err);
  }
}

function boundsForNodes(nodes) {
  const xs = nodes.map(n => Number(n.position?.x || 0));
  const ys = nodes.map(n => Number(n.position?.y || 0));
  return {
    minX: Math.min(...xs),
    minY: Math.min(...ys),
    maxX: Math.max(...xs),
    maxY: Math.max(...ys),
  };
}

function internalEdgesForNodeIds(nodeIds) {
  const keep = new Set(nodeIds || []);
  return state.edges.filter(e => keep.has(e.from_node) && keep.has(e.to_node));
}

function externalPortsForNodeIds(nodeIds) {
  const keep = new Set(nodeIds || []);
  const incoming = [];
  const outgoing = [];
  for (const e of state.edges) {
    if (!keep.has(e.from_node) && keep.has(e.to_node)) incoming.push({ to_node: e.to_node, to_port: e.to_port, from_kind: outputDescriptor(e.from_node, e.from_port)?.kind || '' });
    if (keep.has(e.from_node) && !keep.has(e.to_node)) outgoing.push({ from_node: e.from_node, from_port: e.from_port, kind: outputDescriptor(e.from_node, e.from_port)?.kind || '' });
  }
  return { incoming, outgoing };
}

function makeWorkflowTemplate(nodeIds, name) {
  const keep = new Set(nodeIds || []);
  const nodes = state.nodes.filter(n => keep.has(n.id));
  if (nodes.length < 2) throw new Error('Select at least two connected nodes.');
  const b = boundsForNodes(nodes);
  const edges = internalEdgesForNodeIds(nodeIds);
  const ports = externalPortsForNodeIds(nodeIds);
  return {
    id: uuid('workflow'),
    name: String(name || 'Workflow').trim() || 'Workflow',
    created_at: new Date().toISOString(),
    node_count: nodes.length,
    edge_count: edges.length,
    nodes: nodes.map(n => ({
      id: n.id,
      type: n.type,
      params: deepClone(n.params || {}),
      position: {
        x: Number(n.position?.x || 0) - b.minX,
        y: Number(n.position?.y || 0) - b.minY,
      },
    })),
    edges: edges.map(e => ({ ...e })),
    external_inputs: ports.incoming,
    external_outputs: ports.outgoing,
  };
}

function selectUpstreamWorkflow(nodeId) {
  const ids = upstreamNodeIds(nodeId);
  state.selectedNodeIds = ids;
  state.selectedNodeId = nodeId;
  updateNodeSelectionClasses();
  renderInspector();
  renderResultsForSelected();
  showHint(`Selected upstream workflow: ${ids.length} node${ids.length === 1 ? '' : 's'}. Right-click again to save or duplicate it.`, 2600);
}

function duplicateSubgraph(nodeIds, offset={x: 56, y: 56}) {
  const keep = new Set(nodeIds || []);
  const originals = state.nodes.filter(n => keep.has(n.id));
  if (!originals.length) return [];
  const idMap = {};
  const copies = originals.map(n => {
    const id = nextNodeId(n.type);
    idMap[n.id] = id;
    return {
      ...deepClone(n),
      id,
      params: deepClone(n.params || {}),
      position: {
        x: Number(n.position?.x || 0) + Number(offset.x || 0),
        y: Number(n.position?.y || 0) + Number(offset.y || 0),
      },
    };
  });
  const edges = internalEdgesForNodeIds(nodeIds).map(e => ({
    ...deepClone(e),
    id: uuid('edge'),
    from_node: idMap[e.from_node],
    to_node: idMap[e.to_node],
  }));
  state.nodes.push(...copies);
  state.edges.push(...edges);
  state.selectedNodeIds = copies.map(n => n.id);
  state.selectedNodeId = state.selectedNodeIds[state.selectedNodeIds.length - 1] || null;
  for (const id of state.selectedNodeIds) {
    delete state.nodeResults[id];
    delete state.nodeStatuses[id];
  }
  renderAll();
  return copies;
}

function duplicateSelectedWorkflow() {
  normalizeSelectedNodes();
  const ids = state.selectedNodeIds || [];
  if (ids.length < 2) {
    showHint('Select at least two nodes first. Use Shift/Ctrl-click or "Select upstream workflow".', 2600);
    return;
  }
  const copies = duplicateSubgraph(ids, { x: 64, y: 64 });
  showHint(`Duplicated workflow: ${copies.length} nodes`, 2000);
}

function saveSelectedAsWorkflow() {
  normalizeSelectedNodes();
  const ids = state.selectedNodeIds || [];
  if (ids.length < 2) {
    showHint('Select at least two nodes first.', 2200);
    return;
  }
  const defaultName = `Workflow ${state.workflowTemplates.length + 1}`;
  const name = prompt('Workflow name:', defaultName);
  if (name === null) return;
  try {
    const tpl = makeWorkflowTemplate(ids, name);
    state.workflowTemplates.push(tpl);
    persistWorkflowTemplates();
    renderPalette();
    showHint(`Saved workflow "${tpl.name}" (${tpl.node_count} nodes).`, 2600);
  } catch (err) {
    alert(String(err));
  }
}

function instantiateWorkflow(templateId) {
  const tpl = (state.workflowTemplates || []).find(t => t.id === templateId);
  if (!tpl) return;
  const baseX = canvas.scrollLeft + 90 + (state.nodeCounter % 4) * 40;
  const baseY = canvas.scrollTop + 90 + (state.nodeCounter % 5) * 40;
  const idMap = {};
  const copies = (tpl.nodes || []).map(n => {
    const id = nextNodeId(n.type);
    idMap[n.id] = id;
    return {
      id,
      type: n.type,
      params: deepClone(n.params || {}),
      position: {
        x: baseX + Number(n.position?.x || 0),
        y: baseY + Number(n.position?.y || 0),
      },
    };
  });
  const edges = (tpl.edges || [])
    .filter(e => idMap[e.from_node] && idMap[e.to_node])
    .map(e => ({
      id: uuid('edge'),
      from_node: idMap[e.from_node],
      from_port: e.from_port,
      to_node: idMap[e.to_node],
      to_port: e.to_port,
    }));
  state.nodes.push(...copies);
  state.edges.push(...edges);
  state.selectedNodeIds = copies.map(n => n.id);
  state.selectedNodeId = state.selectedNodeIds[state.selectedNodeIds.length - 1] || null;
  renderAll();
  showHint(`Inserted workflow "${tpl.name}" (${copies.length} nodes). Connect new inputs and run.`, 2800);
}

function deleteWorkflowTemplate(templateId) {
  const tpl = (state.workflowTemplates || []).find(t => t.id === templateId);
  if (!tpl) return;
  if (!confirm(`Delete workflow template "${tpl.name}"?`)) return;
  state.workflowTemplates = state.workflowTemplates.filter(t => t.id !== templateId);
  persistWorkflowTemplates();
  renderPalette();
}



document.addEventListener('click', (ev) => {
  if (!ev.target.closest('.node-context-menu')) closeContextMenu();
});

document.addEventListener('keydown', (ev) => {
  if (ev.key === 'Escape') closeContextMenu();
});


async function init() {
  const nt = await api('/api/node-types');
  state.nodeTypes = nt.node_types;
  state.nodeTypeById = Object.fromEntries(state.nodeTypes.map(n => [n.type, n]));
  state.workflowTemplates = loadWorkflowTemplates();
  renderPalette();
  await refreshExamples();
  await refreshUploads();
  renderAll();
}

function renderPalette() {
  const root = el('nodePalette');
  root.innerHTML = '';

  if ((state.workflowTemplates || []).length) {
    const wc = document.createElement('div');
    wc.className = 'palette-category workflow-category';
    wc.textContent = 'Custom Workflows';
    root.appendChild(wc);

    for (const wf of state.workflowTemplates || []) {
      const wrap = document.createElement('div');
      wrap.className = 'palette-workflow';
      wrap.innerHTML = `
        <button type="button" class="palette-node workflow-insert">
          <span class="title">${escapeHtml(wf.name || 'Workflow')}</span>
          <span class="desc">${escapeHtml(`${wf.node_count || 0} nodes, ${wf.edge_count || 0} internal edges`)}</span>
        </button>
        <button type="button" class="workflow-delete" title="Delete workflow template">×</button>
      `;
      wrap.querySelector('.workflow-insert').onclick = () => instantiateWorkflow(wf.id);
      wrap.querySelector('.workflow-delete').onclick = (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        deleteWorkflowTemplate(wf.id);
      };
      root.appendChild(wrap);
    }
  } else {
    const wc = document.createElement('div');
    wc.className = 'palette-category workflow-category';
    wc.textContent = 'Custom Workflows';
    root.appendChild(wc);
    const empty = document.createElement('div');
    empty.className = 'workflow-empty';
    empty.textContent = 'Right-click selected nodes → Save selected as workflow';
    root.appendChild(empty);
  }

  const categories = [...new Set(state.nodeTypes.map(n => n.category))];
  for (const cat of categories) {
    const c = document.createElement('div');
    c.className = 'palette-category';
    c.textContent = cat;
    root.appendChild(c);
    for (const nt of state.nodeTypes.filter(n => n.category === cat)) {
      const btn = document.createElement('button');
      btn.className = 'palette-node';
      btn.innerHTML = `<span class="title">${escapeHtml(nt.title)}</span><span class="desc">${escapeHtml(nt.description || '')}</span>`;
      btn.onclick = () => addNode(nt.type);
      root.appendChild(btn);
    }
  }
}


function applyLoadedProject(project, out={}) {
  const graph = project.graph || project;
  if (!graph.nodes || !graph.edges) {
    throw new Error('Loaded project does not contain a valid graph.');
  }
  state.nodes = graph.nodes || [];
  state.edges = graph.edges || [];
  state.selectedNodeId = null;
  state.selectedNodeIds = [];
  if (Array.isArray(project.custom_workflows)) {
    state.workflowTemplates = project.custom_workflows;
    persistWorkflowTemplates();
    renderPalette();
  }
  state.nodeResults = {};
  state.nodeStatuses = project.node_statuses || {};
  state.runLog = project.last_run_log || [];
  state.lastManifest = project.last_manifest || null;
  state.lastExecution = null;
  state.nodeCounter = Math.max(1, ...state.nodes.map(n => {
    const m = String(n.id || '').match(/_(\d+)$/);
    return m ? Number(m[1]) + 1 : 1;
  }));
}

async function refreshExamples() {
  const root = el('examplesList');
  if (!root) return;
  try {
    const data = await api('/api/examples');
    state.examples = data.examples || [];
  } catch (err) {
    console.warn('Could not load examples:', err);
    state.examples = [];
  }
  renderExamples();
}

function renderExamples() {
  const root = el('examplesList');
  if (!root) return;
  root.innerHTML = '';
  if (!state.examples.length) {
    root.innerHTML = '<div class="empty">No bundled examples available.</div>';
    return;
  }
  for (const ex of state.examples) {
    const div = document.createElement('div');
    div.className = 'small-item example-item';
    const files = Array.isArray(ex.data_files) && ex.data_files.length
      ? `<br><span class="muted">${escapeHtml(ex.data_files.join(', '))}</span>`
      : '';
    div.innerHTML = `
      <b>${escapeHtml(ex.title || ex.id)}</b>
      <br><span class="muted">${escapeHtml(ex.description || '')}</span>
      ${files}
      <div class="example-actions">
        <button type="button" class="mini primary">Load</button>
      </div>
    `;
    div.querySelector('button').onclick = () => loadExample(ex.id);
    root.appendChild(div);
  }
}

async function loadExample(exampleId) {
  const ex = state.examples.find(item => item.id === exampleId);
  const label = ex ? (ex.title || ex.id) : exampleId;
  if (state.nodes.length || state.edges.length) {
    const ok = confirm(`Load "${label}"? This will replace the current graph. Save your current project first if needed.`);
    if (!ok) return;
  }
  try {
    const out = await api(`/api/examples/${encodeURIComponent(exampleId)}/load`, { method: 'POST' });
    const project = out.project || {};
    applyLoadedProject(project, out);
    await refreshUploads();
    renderAll();

    const missing = out.missing_files || [];
    const imported = out.imported_files || [];
    hint.textContent = missing.length
      ? `Example loaded with missing files: ${missing.join(', ')}`
      : `Example loaded: ${label}. Files imported: ${imported.length}.`;
    hint.classList.remove('hidden');
    setTimeout(() => hint.classList.add('hidden'), 4500);
  } catch (err) {
    alert(String(err));
  }
}



function fileCategory(file) {
  return String(file?.category || file?.folder || (file?.kind === 'output' ? 'Outputs' : 'Uncategorized')).trim() || 'Uncategorized';
}

function uploadCategories() {
  return [...new Set((state.uploads || []).map(fileCategory))].sort((a, b) => a.localeCompare(b));
}

function refreshUploadCategoryOptions() {
  const list = el('uploadCategoryOptions');
  if (!list) return;
  const cats = uploadCategories();
  list.innerHTML = '';
  for (const cat of cats) {
    const opt = document.createElement('option');
    opt.value = cat;
    list.appendChild(opt);
  }
}

function addLoadUploadedFileNode(file, position=null) {
  if (!file || !file.file_id) return null;
  const nt = state.nodeTypeById.LoadUploadedFile;
  if (!nt) {
    alert('Load Uploaded File node type is not available.');
    return null;
  }
  const id = nextNodeId('LoadUploadedFile');
  const pos = position || {
    x: canvas.scrollLeft + 100 + (state.nodeCounter % 5) * 35,
    y: canvas.scrollTop + 100 + (state.nodeCounter % 5) * 35,
  };
  const params = defaultParams(nt);
  params.file_id = file.file_id;
  params.file_type = params.file_type || 'auto';
  const node = {
    id,
    type: 'LoadUploadedFile',
    params,
    position: { x: Math.max(0, pos.x), y: Math.max(0, pos.y) },
  };
  state.nodes.push(node);
  selectNode(id, { rerenderNodes: true });
  showHint(`Created Load Uploaded File for ${file.original_name || file.file_id}`, 2200);
  return node;
}

function canvasPositionFromClient(ev) {
  const cr = canvas.getBoundingClientRect();
  return {
    x: ev.clientX - cr.left + canvas.scrollLeft,
    y: ev.clientY - cr.top + canvas.scrollTop,
  };
}


async function updateUploadFileCategory(file, newCategory) {
  if (!file || !file.file_id) return;
  const category = String(newCategory || 'Uncategorized').trim() || 'Uncategorized';
  try {
    const out = await api('/api/uploads/category', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file_id: file.file_id, category }),
    });
    state.uploads = out.files || state.uploads;
    renderUploadList();
    renderInspector();
    showHint(`Moved ${file.original_name || file.file_id} to ${category}`, 2200);
  } catch (err) {
    alert(String(err));
  }
}

function renderUploadFileItem(file) {
  const div = document.createElement('div');
  div.className = 'small-item upload-file-item';
  div.draggable = true;
  div.dataset.fileId = file.file_id;
  div.title = 'Click or drag to canvas to create a Load Uploaded File node';
  const suffix = file.stored_name ? `<br><span class="muted">${escapeHtml(file.stored_name)}</span>` : '';
  div.innerHTML = `
    <b>${escapeHtml(file.original_name)}</b>
    <br><span class="badge">${escapeHtml(file.kind)}</span>
    <span class="badge folder-badge">${escapeHtml(fileCategory(file))}</span>
    ${suffix}
    <div class="file-item-actions">
      <button type="button" class="mini file-move-btn">Move folder</button>
    </div>
    <div class="file-item-hint">click / drag → Load Uploaded File</div>
  `;
  const moveBtn = div.querySelector('.file-move-btn');
  if (moveBtn) {
    moveBtn.onclick = (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const current = fileCategory(file);
      const next = prompt('Folder / category:', current);
      if (next === null) return;
      updateUploadFileCategory(file, next);
    };
  }
  div.onclick = () => addLoadUploadedFileNode(file);
  div.addEventListener('dragstart', ev => {
    ev.dataTransfer.setData('application/x-gempy-upload-file-id', file.file_id);
    ev.dataTransfer.setData('text/plain', file.file_id);
    ev.dataTransfer.effectAllowed = 'copy';
  });
  return div;
}

function renderUploadList() {
  const root = el('uploadList');
  root.innerHTML = '';
  refreshUploadCategoryOptions();
  if (!state.uploads.length) {
    root.innerHTML = '<div class="empty">No uploaded files available. If you manually deleted files, stale entries have been removed automatically.</div>';
    return;
  }

  const groups = new Map();
  for (const f of state.uploads) {
    const cat = fileCategory(f);
    if (!groups.has(cat)) groups.set(cat, []);
    groups.get(cat).push(f);
  }

  const sortedCats = [...groups.keys()].sort((a, b) => {
    if (a === 'Uncategorized') return 1;
    if (b === 'Uncategorized') return -1;
    if (a === 'Outputs') return 1;
    if (b === 'Outputs') return -1;
    return a.localeCompare(b);
  });

  for (const cat of sortedCats) {
    const files = groups.get(cat).sort((a, b) => String(a.original_name || '').localeCompare(String(b.original_name || '')));
    const details = document.createElement('details');
    details.className = 'upload-folder';
    details.open = true;
    const summary = document.createElement('summary');
    summary.innerHTML = `<span>${escapeHtml(cat)}</span><span class="folder-count">${files.length}</span>`;
    details.appendChild(summary);
    const list = document.createElement('div');
    list.className = 'upload-folder-files';
    files.forEach(f => list.appendChild(renderUploadFileItem(f)));
    details.appendChild(list);
    root.appendChild(details);
  }
}

function groupedFileSelectOptions(select, current) {
  const empty = document.createElement('option');
  empty.value = '';
  empty.textContent = '— none / choose file —';
  select.appendChild(empty);
  let foundCurrent = !current;

  const groups = new Map();
  for (const f of state.uploads || []) {
    const cat = fileCategory(f);
    if (!groups.has(cat)) groups.set(cat, []);
    groups.get(cat).push(f);
  }

  for (const cat of [...groups.keys()].sort((a, b) => a.localeCompare(b))) {
    const optgroup = document.createElement('optgroup');
    optgroup.label = cat;
    const files = groups.get(cat).sort((a, b) => String(a.original_name || '').localeCompare(String(b.original_name || '')));
    for (const f of files) {
      const o = document.createElement('option');
      o.value = f.file_id;
      o.textContent = f.original_name;
      if (f.file_id === current) {
        o.selected = true;
        foundCurrent = true;
      }
      optgroup.appendChild(o);
    }
    select.appendChild(optgroup);
  }

  if (current && !foundCurrent) {
    const missing = document.createElement('option');
    missing.value = current;
    missing.textContent = `Missing file reference: ${current}`;
    missing.selected = true;
    missing.className = 'missing-option';
    select.insertBefore(missing, select.children[1] || null);
  }
}

function setRunButtonsRunning(running) {
  const runBtn = el('btnRun');
  const runAllBtn = el('btnRunAll');
  const stopBtn = el('btnStopRun');
  if (runBtn) runBtn.disabled = Boolean(running);
  if (runAllBtn) runAllBtn.disabled = Boolean(running);
  if (stopBtn) stopBtn.disabled = !running;
}

function stopProgressPolling() {
  if (state.executionPollTimer) {
    clearInterval(state.executionPollTimer);
    state.executionPollTimer = null;
  }
}

async function pollExecutionProgressOnce() {
  try {
    const out = await api('/api/execute/progress');
    const p = out.progress || {};
    if (p.node_statuses) {
      state.nodeStatuses = { ...state.nodeStatuses, ...p.node_statuses };
    }
    const msg = p.message || '';
    if (msg) {
      hint.textContent = msg;
      hint.classList.remove('hidden');
    }
    renderNodes();
    renderRunLog();
    const pState = String(p.state || '');
    if (p.run_id && !['idle', 'done'].includes(pState)) {
      state.activeProgressRunId = p.run_id;
    }
    const finished = ['done', 'failed', 'cancelled'].includes(pState);
    const sameRunFinished = finished && state.activeProgressRunId && p.run_id === state.activeProgressRunId;
    if (sameRunFinished && state.runInProgress) {
      stopProgressPolling();
      state.runInProgress = false;
      setRunButtonsRunning(false);
      setTimeout(() => hint.classList.add('hidden'), 2500);
    }
    return p;
  } catch (err) {
    console.warn('Progress polling failed:', err);
  }
}

function startProgressPolling() {
  stopProgressPolling();
  state.activeProgressRunId = null;
  state.progressPollStartedAt = Date.now();
  state.executionPollTimer = setInterval(pollExecutionProgressOnce, 650);
  setTimeout(pollExecutionProgressOnce, 250);
}

async function stopCurrentRun() {
  const stopBtn = el('btnStopRun');
  if (stopBtn) stopBtn.disabled = true;
  try {
    const out = await api('/api/execute/cancel', { method: 'POST' });
    const p = out.progress || {};
    if (p.message) showHint(p.message, 3500);
    if (p.node_statuses) state.nodeStatuses = { ...state.nodeStatuses, ...p.node_statuses };
    renderNodes();
    renderRunLog();
  } catch (err) {
    alert(String(err));
    if (stopBtn && state.runInProgress) stopBtn.disabled = false;
  }
}

async function refreshUploads() {
  const data = await api('/api/uploads');
  state.uploads = data.files || [];
  renderUploadList();
  renderInspector();
}

async function cleanupMissingFiles() {
  try {
    const out = await api('/api/uploads/cleanup', { method: 'POST' });
    state.uploads = out.files || [];
    await refreshUploads();
    const stats = out.stats || {};
    hint.textContent = `Missing file records cleaned. Removed: ${stats.removed || 0}.`;
    hint.classList.remove('hidden');
    setTimeout(() => hint.classList.add('hidden'), 2800);
  } catch (err) {
    alert(String(err));
  }
}


function defaultParams(nt) {
  const out = {};
  for (const p of nt.params || []) out[p.name] = p.default ?? '';
  return out;
}

function addNode(type) {
  const nt = state.nodeTypeById[type];
  const id = nextNodeId(type);
  const centerX = canvas.scrollLeft + 80 + (state.nodeCounter % 5) * 45;
  const centerY = canvas.scrollTop + 80 + (state.nodeCounter % 6) * 55;
  state.nodes.push({
    id,
    type,
    params: defaultParams(nt),
    position: { x: centerX, y: centerY },
  });
  selectNode(id, { rerenderNodes: true });
}

function selectNode(nodeId, opts={}) {
  if (!nodeId) return;
  const additive = Boolean(opts.additive);
  if (additive) {
    const set = selectedNodeIdSet();
    if (set.has(nodeId)) set.delete(nodeId);
    else set.add(nodeId);
    state.selectedNodeIds = [...set];
    state.selectedNodeId = nodeId;
    if (!state.selectedNodeIds.length) state.selectedNodeId = null;
  } else {
    state.selectedNodeId = nodeId;
    state.selectedNodeIds = [nodeId];
  }
  normalizeSelectedNodes();
  if (opts.rerenderNodes) renderNodes();
  else updateNodeSelectionClasses();
  renderInspector();
  renderResultsForSelected();
  renderRunLog();
}

function updateNodeSelectionClasses() {
  const selected = selectedNodeIdSet();
  for (const div of canvas.querySelectorAll('.node')) {
    div.classList.toggle('selected', selected.has(div.dataset.nodeId));
  }
}


function nodeStatus(nodeId) {
  return state.nodeStatuses[nodeId] || { status: 'idle' };
}
function nodeStatusLabel(nodeId) {
  const s = nodeStatus(nodeId);
  if (!s || !s.status || s.status === 'idle') return 'not run';
  if (s.status === 'done') return `${s.duration_ms ?? 0} ms`;
  if (s.status === 'cached') return 'cached';
  if (s.status === 'failed') return 'failed';
  if (s.status === 'queued') return 'queued';
  if (s.status === 'running') return 'running';
  if (s.status === 'cancelled') return 'cancelled';
  if (s.status === 'cancelling') return 'stopping';
  return s.status;
}
function markNodesRunning(nodeIds) {
  const set = new Set(nodeIds || []);
  for (const n of state.nodes) {
    if (set.has(n.id)) state.nodeStatuses[n.id] = { status: 'queued', type: n.type, title: state.nodeTypeById[n.type]?.title || n.type };
  }
  renderNodes();
}

function deleteNode(nodeId) {
  if (!nodeId) return;
  state.nodes = state.nodes.filter(n => n.id !== nodeId);
  state.edges = state.edges.filter(e => e.from_node !== nodeId && e.to_node !== nodeId);
  delete state.nodeResults[nodeId];
  delete state.nodeStatuses[nodeId];
  state.selectedNodeIds = (state.selectedNodeIds || []).filter(id => id !== nodeId);
  if (state.selectedNodeId === nodeId) state.selectedNodeId = state.selectedNodeIds[state.selectedNodeIds.length - 1] || null;
  renderAll();
}

function renderAll() {
  renderNodes();
  renderEdges();
  renderInspector();
  renderResultsForSelected();
  renderRunLog();
}

function renderNodes() {
  [...canvas.querySelectorAll('.node')].forEach(n => n.remove());
  for (const n of state.nodes) {
    const nt = state.nodeTypeById[n.type];
    const div = document.createElement('div');
    div.className = 'node ' + nodeStatus(n.id).status + ((state.selectedNodeIds || []).includes(n.id) ? ' selected' : '');
    div.dataset.nodeId = n.id;
    div.style.left = `${n.position.x}px`;
    div.style.top = `${n.position.y}px`;

    const inputs = nt.inputs || [];
    const outputs = nt.outputs || [];
    div.innerHTML = `
      <div class="node-header">
        <div class="node-title">${escapeHtml(nt.title)}</div>
        <span class="node-status ${escapeHtml(nodeStatus(n.id).status)}">${escapeHtml(nodeStatusLabel(n.id))}</span>
        <div class="node-type">${escapeHtml(n.id)}</div>
      </div>
      <div class="node-body">
        ${inputs.map(p => portHtml(n.id, p, 'input')).join('')}
        ${outputs.map(p => portHtml(n.id, p, 'output')).join('')}
      </div>
      <div class="node-actions">
        <button data-action="select">Edit</button>
        <button data-action="delete" class="danger">Delete</button>
      </div>`;
    canvas.appendChild(div);

    div.addEventListener('contextmenu', ev => showNodeContextMenu(ev, n.id));
    div.querySelector('.node-header').addEventListener('mousedown', ev => startDrag(ev, n.id));
    div.addEventListener('mousedown', (ev) => {
      if (ev.target.closest('.port-dot') || ev.target.closest('button')) return;
      if (ev.shiftKey || ev.ctrlKey || ev.metaKey) {
        ev.preventDefault();
        ev.stopPropagation();
        selectNode(n.id, { additive: true });
        return;
      }
      selectNode(n.id);
    });
    div.querySelector('[data-action="select"]').onclick = (ev) => { ev.stopPropagation(); selectNode(n.id, { additive: ev.shiftKey || ev.ctrlKey || ev.metaKey }); };
    div.querySelector('[data-action="delete"]').onclick = (ev) => { ev.stopPropagation(); deleteNode(n.id); };
    for (const dot of div.querySelectorAll('.port-dot')) {
      dot.addEventListener('mousedown', (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
      });
      dot.onclick = (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        handlePortClick(dot.dataset.nodeId, dot.dataset.portName, dot.dataset.direction);
      };
    }
  }
}

function portHtml(nodeId, p, direction) {
  const mult = p.multiple ? ' *' : '';
  if (direction === 'input') {
    return `<div class="port-row input">
      <span class="port-dot input" data-node-id="${nodeId}" data-port-name="${p.name}" data-direction="input" title="input ${p.kind}"></span>
      <span class="port-label">${escapeHtml(p.label)}${mult}</span>
      <span class="port-kind">${escapeHtml(p.kind)}</span>
    </div>`;
  }
  return `<div class="port-row output">
      <span class="port-kind">${escapeHtml(p.kind)}</span>
      <span class="port-label">${escapeHtml(p.label)}</span>
      <span class="port-dot output" data-node-id="${nodeId}" data-port-name="${p.name}" data-direction="output" title="output ${p.kind}"></span>
    </div>`;
}

function handlePortClick(nodeId, portName, direction) {
  if (direction === 'output') {
    state.pendingConnection = { nodeId, portName };
    hint.textContent = `Connecting from ${nodeId}.${portName}; click an input port.`;
    hint.classList.remove('hidden');
    renderPortActive();
    return;
  }
  if (!state.pendingConnection) {
    hint.textContent = 'Click an output port first, then an input port.';
    hint.classList.remove('hidden');
    setTimeout(() => hint.classList.add('hidden'), 1800);
    return;
  }
  const fromNode = state.nodes.find(n => n.id === state.pendingConnection.nodeId);
  const toNode = state.nodes.find(n => n.id === nodeId);
  if (!fromNode || !toNode) return;
  if (fromNode.id === toNode.id) {
    endConnection('Cannot connect a node to itself.');
    return;
  }
  const fromMeta = outputSpec(fromNode.type, state.pendingConnection.portName);
  const toMeta = inputSpec(toNode.type, portName);
  if (!fromMeta || !toMeta) {
    endConnection('Invalid port.');
    return;
  }
  if (!compatible(fromMeta.kind, toMeta.kind)) {
    endConnection(`Type mismatch: ${fromMeta.kind} → ${toMeta.kind}`);
    return;
  }
  if (!toMeta.multiple) {
    state.edges = state.edges.filter(e => !(e.to_node === nodeId && e.to_port === portName));
  }
  state.edges.push({
    id: uuid('edge'),
    from_node: fromNode.id,
    from_port: state.pendingConnection.portName,
    to_node: toNode.id,
    to_port: portName,
  });
  state.pendingConnection = null;
  hint.classList.add('hidden');
  renderAll();
}

function endConnection(msg) {
  state.pendingConnection = null;
  hint.textContent = msg;
  hint.classList.remove('hidden');
  setTimeout(() => hint.classList.add('hidden'), 2300);
  renderPortActive();
}

function renderPortActive() {
  for (const d of document.querySelectorAll('.port-dot')) d.classList.remove('active');
  if (state.pendingConnection) {
    const selector = `.port-dot.output[data-node-id="${cssEscape(state.pendingConnection.nodeId)}"][data-port-name="${cssEscape(state.pendingConnection.portName)}"]`;
    const dot = document.querySelector(selector);
    if (dot) dot.classList.add('active');
  }
}

function inputSpec(type, portName) {
  return (state.nodeTypeById[type]?.inputs || []).find(p => p.name === portName);
}
function outputSpec(type, portName) {
  return (state.nodeTypeById[type]?.outputs || []).find(p => p.name === portName);
}
function compatible(a, b) {
  if (a === b) return true;
  if (a === 'any' || b === 'any') return true;
  return false;
}

function startDrag(ev, nodeId) {
  ev.preventDefault();
  ev.stopPropagation();

  if (ev.shiftKey || ev.ctrlKey || ev.metaKey) {
    selectNode(nodeId, { additive: true });
    return;
  }

  if (!(state.selectedNodeIds || []).includes(nodeId)) {
    selectNode(nodeId);
  } else {
    state.selectedNodeId = nodeId;
    updateNodeSelectionClasses();
    renderInspector();
    renderResultsForSelected();
  }

  const dragIds = (state.selectedNodeIds || []).includes(nodeId) ? [...state.selectedNodeIds] : [nodeId];
  const originals = {};
  for (const id of dragIds) {
    const node = state.nodes.find(n => n.id === id);
    if (node) originals[id] = { x: Number(node.position.x || 0), y: Number(node.position.y || 0) };
  }

  state.drag = { nodeIds: dragIds, startX: ev.clientX, startY: ev.clientY, originals };
  document.addEventListener('mousemove', onDrag);
  document.addEventListener('mouseup', stopDrag, { once: true });
}
function onDrag(ev) {
  if (!state.drag) return;
  const dx = ev.clientX - state.drag.startX;
  const dy = ev.clientY - state.drag.startY;
  for (const id of state.drag.nodeIds || []) {
    const node = state.nodes.find(n => n.id === id);
    const orig = state.drag.originals?.[id];
    if (!node || !orig) continue;
    node.position.x = Math.max(0, orig.x + dx);
    node.position.y = Math.max(0, orig.y + dy);
    const div = canvas.querySelector(`.node[data-node-id="${cssEscape(node.id)}"]`);
    if (div) {
      div.style.left = `${node.position.x}px`;
      div.style.top = `${node.position.y}px`;
    }
  }
  renderEdges();
}
function stopDrag() {
  state.drag = null;
  document.removeEventListener('mousemove', onDrag);
}


function startPan(ev) {
  if (ev.button !== 0) return;
  if (ev.target !== canvas) return;
  ev.preventDefault();
  state.pan = {
    startX: ev.clientX,
    startY: ev.clientY,
    startScrollLeft: canvas.scrollLeft,
    startScrollTop: canvas.scrollTop,
  };
  canvas.classList.add('panning');
  document.addEventListener('mousemove', onPan);
  document.addEventListener('mouseup', stopPan, { once: true });
}
function onPan(ev) {
  if (!state.pan) return;
  canvas.scrollLeft = state.pan.startScrollLeft - (ev.clientX - state.pan.startX);
  canvas.scrollTop = state.pan.startScrollTop - (ev.clientY - state.pan.startY);
  renderEdges();
}
function stopPan() {
  state.pan = null;
  canvas.classList.remove('panning');
  document.removeEventListener('mousemove', onPan);
}

function renderEdges() {
  svg.innerHTML = '';
  for (const e of state.edges) {
    const p1 = portCenter(e.from_node, e.from_port, 'output');
    const p2 = portCenter(e.to_node, e.to_port, 'input');
    if (!p1 || !p2) continue;
    const dx = Math.max(80, Math.abs(p2.x - p1.x) * 0.45);
    const d = `M ${p1.x} ${p1.y} C ${p1.x + dx} ${p1.y}, ${p2.x - dx} ${p2.y}, ${p2.x} ${p2.y}`;
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', d);
    path.setAttribute('class', 'edge-path');
    svg.appendChild(path);
  }
}

function portCenter(nodeId, portName, direction) {
  const dot = canvas.querySelector(`.port-dot.${direction}[data-node-id="${cssEscape(nodeId)}"][data-port-name="${cssEscape(portName)}"]`);
  if (!dot) return null;
  const r = dot.getBoundingClientRect();
  const cr = canvas.getBoundingClientRect();
  return { x: r.left - cr.left + canvas.scrollLeft + r.width / 2, y: r.top - cr.top + canvas.scrollTop + r.height / 2 };
}


function connectedInputEdges(node, portNames) {
  if (!node) return [];
  const ports = Array.isArray(portNames) ? new Set(portNames) : new Set([portNames]);
  return state.edges.filter(e => e.to_node === node.id && ports.has(e.to_port));
}

function previewCellScalarsFromDescriptor(desc) {
  const preview = desc?.preview || {};
  const metadata = desc?.metadata || {};
  const values = [];
  const add = (name, assoc='cell') => {
    if (!name) return;
    const key = `${assoc}:${name}`;
    if (!values.some(v => v.key === key || v.name === name)) {
      values.push({ key, name: String(name), association: assoc });
    }
  };
  for (const name of preview.cell_data || []) add(name, 'cell');
  for (const name of metadata.cell_data || []) add(name, 'cell');
  for (const arr of preview.scalarArrays || []) {
    if ((arr.association || '').toLowerCase().includes('cell')) add(arr.name, 'cell');
  }
  // Point data is shown only as a hint, because voxel model values are normally
  // stored as cell data. Backend fallback uses cell data for merge values.
  for (const name of preview.point_data || []) add(name, 'point');
  return values;
}

function parseVoxelScalarMap(raw) {
  try {
    const v = typeof raw === 'string' ? JSON.parse(raw || '[]') : raw;
    return Array.isArray(v) ? v : [];
  } catch {
    return [];
  }
}

function saveVoxelScalarMap(node, prm, rows) {
  node.params[prm.name] = JSON.stringify(rows || [], null, 2);
}

function renderVoxelScalarMapBuilder(node, prm, current) {
  const box = document.createElement('div');
  box.className = 'voxel-scalar-map-builder';

  const intro = document.createElement('div');
  intro.className = 'param-help inline-note';
  intro.textContent = 'Choose the scalar to read from each connected voxel model. The merge node will reindex selected values into the output scalar starting at 1.';
  box.appendChild(intro);

  const edges = connectedInputEdges(node, ['voxel_models', 'voxel_model', 'voxel_grid', 'mesh']);
  const savedRows = parseVoxelScalarMap(current);
  const savedByEdge = new Map();
  for (const row of savedRows) {
    if (!row) continue;
    if (row.edge_id) savedByEdge.set(String(row.edge_id), row);
    if (row.index !== undefined) savedByEdge.set(`idx:${row.index}`, row);
  }

  const rows = [];
  const table = document.createElement('div');
  table.className = 'voxel-scalar-map-table';

  if (!edges.length) {
    const empty = document.createElement('div');
    empty.className = 'inline-preview-empty';
    empty.textContent = 'Connect voxel models to the voxel_models input first. Run upstream voxel nodes once so their cell_data scalar lists are available.';
    box.appendChild(empty);
    saveVoxelScalarMap(node, prm, []);
    return box;
  }

  edges.forEach((edge, idx) => {
    const desc = outputDescriptor(edge.from_node, edge.from_port);
    const scalars = previewCellScalarsFromDescriptor(desc);
    const saved = savedByEdge.get(String(edge.id)) || savedByEdge.get(`idx:${idx}`) || {};
    const sourceName = desc?.name || edge.from_node;
    const currentScalar = saved.scalar || (scalars.some(s => s.name === 'MaterialIDs') ? 'MaterialIDs' : (scalars[0]?.name || 'auto'));

    const row = document.createElement('div');
    row.className = 'voxel-scalar-map-row';

    const label = document.createElement('div');
    label.className = 'voxel-scalar-source';
    label.innerHTML = `<b>#${idx + 1}</b> ${escapeHtml(edge.from_node)}.${escapeHtml(edge.from_port)}<br><span>${escapeHtml(sourceName)}</span>`;
    row.appendChild(label);

    const select = document.createElement('select');
    select.className = 'voxel-scalar-select';

    const auto = document.createElement('option');
    auto.value = 'auto';
    auto.textContent = scalars.length ? 'auto' : 'auto — run upstream to list scalars';
    select.appendChild(auto);

    for (const s of scalars) {
      const opt = document.createElement('option');
      opt.value = s.name;
      opt.textContent = `${s.name} (${s.association})`;
      if (s.name === currentScalar) opt.selected = true;
      select.appendChild(opt);
    }

    if (currentScalar && currentScalar !== 'auto' && !scalars.some(s => s.name === currentScalar)) {
      const opt = document.createElement('option');
      opt.value = currentScalar;
      opt.textContent = `${currentScalar} (saved / not available yet)`;
      opt.selected = true;
      select.appendChild(opt);
    }

    row.appendChild(select);

    const rowObj = {
      index: idx,
      edge_id: edge.id,
      from_node: edge.from_node,
      from_port: edge.from_port,
      to_port: edge.to_port,
      source_name: sourceName,
      scalar: select.value || 'auto',
      available_cell_data: scalars.filter(s => s.association === 'cell').map(s => s.name),
    };
    rows.push(rowObj);

    select.onchange = () => {
      rowObj.scalar = select.value || 'auto';
      saveVoxelScalarMap(node, prm, rows);
    };

    table.appendChild(row);
  });

  box.appendChild(table);

  const actions = document.createElement('div');
  actions.className = 'builder-actions';
  const refresh = document.createElement('button');
  refresh.type = 'button';
  refresh.textContent = '↻ Refresh scalar list';
  refresh.onclick = (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    renderInspector();
  };
  actions.appendChild(refresh);
  box.appendChild(actions);

  saveVoxelScalarMap(node, prm, rows);
  return box;
}


function renderInspector() {
  const root = el('inspectorBody');
  const node = state.nodes.find(n => n.id === state.selectedNodeId);
  if (!node) {
    root.className = 'empty';
    root.textContent = 'Select a node.';
    return;
  }
  root.className = '';
  const nt = state.nodeTypeById[node.type];
  const selectedCount = (state.selectedNodeIds || []).length;
  root.innerHTML = `<div class="result-title">${escapeHtml(nt.title)} <span>${escapeHtml(node.id)}${selectedCount > 1 ? ` · ${selectedCount} selected` : ''}</span></div>`;

  if ((state.selectedNodeIds || []).length > 1) {
    const wfPanel = document.createElement('div');
    wfPanel.className = 'workflow-selection-panel';
    wfPanel.innerHTML = `
      <div><b>${state.selectedNodeIds.length}</b> nodes selected</div>
      <div class="workflow-selection-actions">
        <button type="button" data-action="duplicate-selected">Duplicate workflow</button>
        <button type="button" data-action="save-selected">Save as workflow</button>
      </div>
    `;
    wfPanel.querySelector('[data-action="duplicate-selected"]').onclick = duplicateSelectedWorkflow;
    wfPanel.querySelector('[data-action="save-selected"]').onclick = saveSelectedAsWorkflow;
    root.appendChild(wfPanel);
  }

  for (const prm of nt.params || []) {
    root.appendChild(renderParam(prm, node));
  }

  if (node.type === 'Visualization' || node.type === 'PlotGemPy3D' || node.type === 'InteractiveGeoDataEditor' || node.type === 'InteractiveGemPyModelDataEditor') {
    const section = document.createElement('div');
    section.className = 'inline-preview-section';
    const title = document.createElement('div');
    title.className = 'inline-preview-title';
    title.textContent = 'Preview';
    section.appendChild(title);

    const result = state.nodeResults[node.id];
    if (!result) {
      const empty = document.createElement('div');
      empty.className = 'inline-preview-empty';
      empty.textContent = node.type === 'PlotGemPy3D' ? 'Run to this Plot GemPy 3D node to show the model here.' : ((node.type === 'InteractiveGeoDataEditor' || node.type === 'InteractiveGemPyModelDataEditor') ? 'Run this editor node once to load the interactive point cloud.' : 'Run to this Visualization node to show the connected data here.');
      section.appendChild(empty);
    } else if (!result.ok) {
      section.appendChild(errorCard('Preview failed', result.error || 'Unknown error'));
    } else {
      const firstOutput = Object.values(result.outputs || {})[0];
      if (firstOutput) {
        section.appendChild(renderPreview(firstOutput.preview, { mode: 'inline', showActions: true, compactJson: true }));
      } else {
        const empty = document.createElement('div');
        empty.className = 'inline-preview-empty';
        empty.textContent = 'No preview output.';
        section.appendChild(empty);
      }
    }
    root.appendChild(section);
  }
}

function renderParam(prm, node) {
  const wrap = document.createElement('div');
  wrap.className = 'param';
  const label = document.createElement('label');
  label.textContent = prm.label;
  wrap.appendChild(label);
  const current = node.params[prm.name] ?? prm.default ?? '';

  const kind = prm.kind || '';
  if (kind === 'extent6') {
    wrap.appendChild(renderExtentControl(node, prm, current));
  } else if (kind === 'resolution3' || kind === 'shape3') {
    wrap.appendChild(renderOptionalShapeControl(node, prm, current, kind === 'resolution3' ? 'Use explicit resolution' : 'Use reshape'));
  } else if (kind === 'mapping_builder' || prm.name === 'mapping_json') {
    wrap.appendChild(renderMappingBuilder(node, prm, current));
  } else if (kind === 'structural_groups' || prm.name === 'groups_json') {
    wrap.appendChild(renderStructuralGroupsBuilder(node, prm, current));
  } else if (kind === 'fault_relations_builder' || prm.name === 'fault_relations_json') {
    wrap.appendChild(renderFaultRelationsBuilder(node, prm, current));
  } else if (kind === 'finite_fault_builder' || prm.name === 'finite_faults_json') {
    wrap.appendChild(renderFiniteFaultBuilder(node, prm, current));
  } else if (kind === 'surface_points_builder' || prm.name === 'points_json') {
    wrap.appendChild(renderSurfacePointsBuilder(node, prm, current));
  } else if (kind === 'geological_point_editor' || prm.name === 'operations_json') {
    wrap.appendChild(renderGeologicalPointEditor(node, prm, current));
  } else if (kind === 'layer_styles' || prm.name === 'layer_styles_json') {
    wrap.appendChild(renderLayerStylesBuilder(node, prm, current));
  } else if (kind === 'voxel_scalar_map' || prm.name === 'input_scalar_map_json') {
    wrap.appendChild(renderVoxelScalarMapBuilder(node, prm, current));
  } else {
    wrap.appendChild(renderBasicParamInput(prm, node, current));
  }

  if (prm.help) {
    const help = document.createElement('div');
    help.className = 'param-help';
    help.textContent = prm.help;
    wrap.appendChild(help);
  }
  return wrap;
}


function parseEditorOperations(raw) {
  try {
    const v = typeof raw === 'string' ? JSON.parse(raw || '[]') : raw;
    return Array.isArray(v) ? v : [];
  } catch {
    return [];
  }
}

function setEditorOperations(node, prm, operations) {
  node.params[prm.name] = JSON.stringify(operations || []);
}

function getInteractiveEditorPreview(node) {
  const result = state.nodeResults[node.id];
  if (!result || !result.ok) return null;
  const outputs = result.outputs || {};
  return outputs.report?.preview || outputs.preview?.preview || Object.values(outputs)[0]?.preview || null;
}

function formatPointLabel(p) {
  if (!p) return '';
  const tag = p.kind === 'orientation' ? 'ORI' : 'SP';
  return `${tag} | ${p.element || ''} | ${Number(p.x).toFixed(3)}, ${Number(p.y).toFixed(3)}, ${Number(p.z).toFixed(3)}`;
}

function renderGeologicalPointEditor(node, prm, current) {
  const box = document.createElement('div');
  box.className = 'geo-point-editor';

  const preview = getInteractiveEditorPreview(node);
  const operations = parseEditorOperations(current);
  let selectedPoint = null;
  let lastPickedPosition = null;

  const intro = document.createElement('div');
  intro.className = 'param-help';
  intro.textContent = node.type === 'InteractiveGemPyModelDataEditor' ? 'Run this node once to load points from the existing GeoModel. Select points in 3D or from the list. Queued edits are applied to the GeoModel through GemPy API calls when you run the node again.' : 'Run this node once, then select points in the 3D preview or from the list. Edits are queued here and applied when you run the node again.';
  box.appendChild(intro);

  if (!preview || !Array.isArray(preview.editor_points)) {
    const empty = document.createElement('div');
    empty.className = 'inline-preview-empty';
    empty.textContent = 'No editable point cloud yet. Connect surface points/orientations tables, then run this node.';
    box.appendChild(empty);
    return box;
  }

  const points = preview.editor_points || [];
  const formations = preview.formations || [...new Set(points.map(p => p.element).filter(Boolean))];

  const viewerWrap = document.createElement('div');
  viewerWrap.className = 'geo-point-editor-viewer';
  viewerWrap.appendChild(renderWebVTKViewer(preview, { inline: true }));
  viewerWrap.addEventListener('vtk-point-picked', (ev) => {
    const detail = ev.detail || {};
    if (detail.position) lastPickedPosition = detail.position;
    if (detail.point) selectPoint(detail.point);
  });
  box.appendChild(viewerWrap);

  const status = document.createElement('div');
  status.className = 'geo-editor-status';
  status.textContent = `Loaded ${preview.surface_count || 0} surface points and ${preview.orientation_count || 0} orientations. Click a point or choose it from the list.`;
  box.appendChild(status);

  const select = document.createElement('select');
  select.className = 'geo-editor-point-select';
  const none = document.createElement('option');
  none.value = '';
  none.textContent = '— select point/orientation —';
  select.appendChild(none);
  points.forEach((p, idx) => {
    const opt = document.createElement('option');
    opt.value = String(idx);
    opt.textContent = `${idx}: ${formatPointLabel(p)}`;
    select.appendChild(opt);
  });
  box.appendChild(select);

  const form = document.createElement('div');
  form.className = 'geo-editor-form';
  form.innerHTML = `
    <div class="geo-editor-row">
      <label>Kind <select data-field="kind"><option value="surface">surface point</option><option value="orientation">orientation</option></select></label>
      <label>Element <input data-field="element" list="geo-editor-elements-${cssEscape(node.id)}" placeholder="formation / element"></label>
      <datalist id="geo-editor-elements-${cssEscape(node.id)}">${formations.map(f => `<option value="${escapeHtml(f)}"></option>`).join('')}</datalist>
    </div>
    <div class="geo-editor-row">
      <label>X <input data-field="x" type="number" step="any"></label>
      <label>Y <input data-field="y" type="number" step="any"></label>
      <label>Z <input data-field="z" type="number" step="any"></label>
    </div>
    <div class="geo-editor-row orientation-fields">
      <label>G_x <input data-field="gx" type="number" step="any" value="0"></label>
      <label>G_y <input data-field="gy" type="number" step="any" value="0"></label>
      <label>G_z <input data-field="gz" type="number" step="any" value="1"></label>
    </div>
  `;
  box.appendChild(form);

  const field = (name) => form.querySelector(`[data-field="${name}"]`);
  const readForm = () => ({
    kind: field('kind').value,
    element: field('element').value.trim(),
    x: Number(field('x').value),
    y: Number(field('y').value),
    z: Number(field('z').value),
    gx: Number(field('gx').value || 0),
    gy: Number(field('gy').value || 0),
    gz: Number(field('gz').value || 1),
  });

  function fillFormFromPoint(p) {
    if (!p) return;
    field('kind').value = p.kind || 'surface';
    field('element').value = p.element || '';
    field('x').value = p.x ?? '';
    field('y').value = p.y ?? '';
    field('z').value = p.z ?? '';
    field('gx').value = p.gx ?? 0;
    field('gy').value = p.gy ?? 0;
    field('gz').value = p.gz ?? 1;
  }

  function selectPoint(p) {
    selectedPoint = p;
    fillFormFromPoint(p);
    status.textContent = `Selected: ${formatPointLabel(p)}. Modify/delete it, or use its coordinates as the location for a new point.`;
    const idx = points.findIndex(q => q.uid === p.uid && q.kind === p.kind);
    if (idx >= 0) select.value = String(idx);
  }

  select.onchange = () => {
    const idx = Number(select.value);
    if (Number.isInteger(idx) && points[idx]) selectPoint(points[idx]);
  };

  const actions = document.createElement('div');
  actions.className = 'geo-editor-actions';
  const btnModify = document.createElement('button');
  btnModify.type = 'button';
  btnModify.textContent = 'Queue modify selected';
  const btnDelete = document.createElement('button');
  btnDelete.type = 'button';
  btnDelete.className = 'danger';
  btnDelete.textContent = 'Queue delete selected';
  const btnAddSurface = document.createElement('button');
  btnAddSurface.type = 'button';
  btnAddSurface.textContent = 'Queue add surface point';
  const btnAddOrientation = document.createElement('button');
  btnAddOrientation.type = 'button';
  btnAddOrientation.textContent = 'Queue add orientation';
  const btnPicked = document.createElement('button');
  btnPicked.type = 'button';
  btnPicked.textContent = 'Use last picked position';
  const btnClear = document.createElement('button');
  btnClear.type = 'button';
  btnClear.className = 'secondary';
  btnClear.textContent = 'Clear queued edits';
  const btnRun = document.createElement('button');
  btnRun.type = 'button';
  btnRun.className = 'primary';
  btnRun.textContent = 'Run selected chain to apply';

  [btnModify, btnDelete, btnAddSurface, btnAddOrientation, btnPicked, btnClear, btnRun].forEach(b => actions.appendChild(b));
  box.appendChild(actions);

  const opList = document.createElement('div');
  opList.className = 'geo-editor-op-list';
  box.appendChild(opList);

  function refreshOperations() {
    setEditorOperations(node, prm, operations);
    opList.innerHTML = `<b>Queued edits: ${operations.length}</b>`;
    if (!operations.length) {
      const note = document.createElement('div');
      note.className = 'param-help';
      note.textContent = 'No edits queued.';
      opList.appendChild(note);
      return;
    }
    const list = document.createElement('ol');
    operations.forEach((op, idx) => {
      const li = document.createElement('li');
      li.textContent = `${op.op} ${op.kind || ''} ${op.element || op.formation || op.uid || ''}`;
      const rm = document.createElement('button');
      rm.type = 'button';
      rm.className = 'mini danger';
      rm.textContent = 'remove';
      rm.onclick = () => {
        operations.splice(idx, 1);
        refreshOperations();
      };
      li.appendChild(rm);
      list.appendChild(li);
    });
    opList.appendChild(list);
  }

  btnModify.onclick = () => {
    if (!selectedPoint) {
      alert('Select a point or orientation first.');
      return;
    }
    const f = readForm();
    if (!Number.isFinite(f.x) || !Number.isFinite(f.y) || !Number.isFinite(f.z) || !f.element) {
      alert('X, Y, Z and Element are required.');
      return;
    }
    operations.push({
      op: 'modify',
      kind: selectedPoint.kind,
      uid: selectedPoint.uid,
      X: f.x, Y: f.y, Z: f.z,
      element: f.element,
      G_x: f.gx, G_y: f.gy, G_z: f.gz,
    });
    refreshOperations();
  };

  btnDelete.onclick = () => {
    if (!selectedPoint) {
      alert('Select a point or orientation first.');
      return;
    }
    operations.push({ op: 'delete', kind: selectedPoint.kind, uid: selectedPoint.uid });
    refreshOperations();
  };

  function addQueued(kind) {
    const f = readForm();
    if (!Number.isFinite(f.x) || !Number.isFinite(f.y) || !Number.isFinite(f.z) || !f.element) {
      alert('X, Y, Z and Element are required. Select an existing point first or type/edit coordinates.');
      return;
    }
    operations.push({
      op: kind === 'orientation' ? 'add_orientation' : 'add_surface',
      kind,
      new_uid: `${kind === 'orientation' ? 'o' : 's'}_new_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`,
      X: f.x, Y: f.y, Z: f.z,
      element: f.element,
      G_x: f.gx, G_y: f.gy, G_z: f.gz,
    });
    refreshOperations();
  }

  btnAddSurface.onclick = () => addQueued('surface');
  btnAddOrientation.onclick = () => addQueued('orientation');

  btnPicked.onclick = () => {
    if (lastPickedPosition) {
      field('x').value = lastPickedPosition.x;
      field('y').value = lastPickedPosition.y;
      field('z').value = lastPickedPosition.z;
    } else if (selectedPoint) {
      fillFormFromPoint(selectedPoint);
    } else {
      alert('Click a point in the 3D viewer first.');
    }
  };

  btnClear.onclick = () => {
    if (!confirm('Clear all queued edits for this editor node?')) return;
    operations.splice(0, operations.length);
    refreshOperations();
  };

  btnRun.onclick = () => runGraph('selected');

  refreshOperations();
  return box;
}


function renderBasicParamInput(prm, node, current) {
  let input;
  if (prm.kind === 'textarea') {
    input = document.createElement('textarea');
    input.value = current;
  } else if (prm.kind === 'select') {
    input = document.createElement('select');
    for (const opt of prm.options || []) {
      const o = document.createElement('option');
      o.value = opt;
      o.textContent = opt;
      if (String(opt) === String(current)) o.selected = true;
      input.appendChild(o);
    }
  } else if (prm.kind === 'boolean') {
    input = document.createElement('input');
    input.type = 'checkbox';
    input.checked = !!current && String(current).toLowerCase() !== 'false';
  } else if (prm.kind === 'number') {
    input = document.createElement('input');
    input.type = 'number';
    input.step = 'any';
    input.value = current;
  } else if (prm.kind === 'file_select') {
    input = document.createElement('select');
    groupedFileSelectOptions(input, current);
  } else {
    input = document.createElement('input');
    input.type = 'text';
    input.value = current;
  }
  input.oninput = () => {
    node.params[prm.name] = prm.kind === 'boolean' ? input.checked : input.value;
  };
  input.onchange = input.oninput;
  return input;
}


function extentFromPreview(preview) {
  if (!preview || typeof preview !== 'object') return null;
  if (Array.isArray(preview.raw_xyz_extent) && preview.raw_xyz_extent.length === 6) {
    return preview.raw_xyz_extent.map(Number);
  }
  if (preview.xyz_bounds && Array.isArray(preview.xyz_bounds.raw_extent)) {
    return preview.xyz_bounds.raw_extent.map(Number);
  }
  if (Array.isArray(preview.suggested_extent_5_percent) && preview.suggested_extent_5_percent.length === 6) {
    // This is already padded, so use it only as a fallback.
    return preview.suggested_extent_5_percent.map(Number);
  }
  return null;
}

function mergeExtents(extents) {
  const arr = (extents || []).filter(e => Array.isArray(e) && e.length === 6 && e.every(v => Number.isFinite(Number(v))));
  if (!arr.length) return null;
  return [
    Math.min(...arr.map(e => Number(e[0]))),
    Math.max(...arr.map(e => Number(e[1]))),
    Math.min(...arr.map(e => Number(e[2]))),
    Math.max(...arr.map(e => Number(e[3]))),
    Math.min(...arr.map(e => Number(e[4]))),
    Math.max(...arr.map(e => Number(e[5]))),
  ];
}

function padExtent(extent, paddingPercent=5) {
  if (!Array.isArray(extent) || extent.length !== 6) return null;
  const p = Math.max(Number(paddingPercent || 0), 0) / 100;
  const out = [];
  for (let i = 0; i < 3; i++) {
    const lo = Number(extent[i * 2]);
    const hi = Number(extent[i * 2 + 1]);
    if (!Number.isFinite(lo) || !Number.isFinite(hi)) return null;
    let pad = (hi - lo) * p;
    if (pad === 0) {
      const scale = Math.max(Math.abs(lo), Math.abs(hi), 1);
      pad = p > 0 ? scale * p : 1;
    }
    out.push(lo - pad, hi + pad);
  }
  return out;
}

function connectedTableExtentInfo(node) {
  const extents = [];
  for (const portName of ['surface_points', 'orientations']) {
    const desc = connectedOutputDescriptor(node, portName);
    const ext = extentFromPreview(desc?.preview);
    if (ext) extents.push(ext);
  }
  const raw = mergeExtents(extents);
  const padding = Number(node?.params?.extent_padding_percent ?? 5);
  const padded = raw ? padExtent(raw, padding) : null;
  return { raw, padded, sourceCount: extents.length, padding };
}


function renderExtentControl(node, prm, current) {
  const autoEnabled =
    node.params.auto_extent_from_data !== false &&
    String(node.params.auto_extent_from_data ?? 'true').toLowerCase() !== 'false';
  const userOverridden =
    node.params.extent_user_overridden === true ||
    String(node.params.extent_user_overridden ?? 'false').toLowerCase() === 'true';

  const autoInfo = connectedTableExtentInfo(node);
  let vals = parseJsonArray(current, [-1, 1, -1, 1, -1, 1], 6);

  if (autoEnabled && !userOverridden && autoInfo.padded) {
    vals = autoInfo.padded;
    node.params[prm.name] = JSON.stringify(vals);
  }

  const names = ['x_min', 'x_max', 'y_min', 'y_max', 'z_min', 'z_max'];
  const box = document.createElement('div');
  box.className = 'extent-control';

  const actions = document.createElement('div');
  actions.className = 'builder-actions extent-actions';

  const fillBtn = document.createElement('button');
  fillBtn.type = 'button';
  fillBtn.textContent = '↻ Fill extent from input data';
  fillBtn.disabled = !autoInfo.padded;

  const manualBtn = document.createElement('button');
  manualBtn.type = 'button';
  manualBtn.textContent = userOverridden ? 'Manual override active' : 'Use manual extent';
  manualBtn.className = userOverridden ? 'mini active' : 'mini';

  actions.appendChild(fillBtn);
  actions.appendChild(manualBtn);
  box.appendChild(actions);

  const note = document.createElement('div');
  note.className = 'param-help inline-note';
  if (autoInfo.padded) {
    note.textContent = `Auto extent from ${autoInfo.sourceCount} connected table(s), padding ${autoInfo.padding}%. Edit any value to switch to manual override.`;
  } else {
    note.textContent = 'Run/connect surface points and orientations first to auto-fill extent from X/Y/Z. You can also edit manually.';
  }
  box.appendChild(note);

  const grid = document.createElement('div');
  grid.className = 'grid-param extent-grid';
  const inputs = [];
  names.forEach((name, i) => {
    const item = document.createElement('div');
    item.innerHTML = `<span>${name}</span>`;
    const inp = document.createElement('input');
    inp.type = 'number';
    inp.step = 'any';
    inp.value = Number.isFinite(Number(vals[i])) ? vals[i] : '';
    item.appendChild(inp);
    grid.appendChild(item);
    inputs.push(inp);
  });
  box.appendChild(grid);

  const save = (manual=false) => {
    const out = inputs.map(inp => Number(inp.value));
    node.params[prm.name] = JSON.stringify(out);
    if (manual) {
      node.params.extent_user_overridden = true;
      manualBtn.textContent = 'Manual override active';
      manualBtn.className = 'mini active';
    }
  };

  inputs.forEach(inp => {
    inp.oninput = () => save(true);
    inp.onchange = () => save(true);
  });

  fillBtn.onclick = (event) => {
    event.preventDefault();
    event.stopPropagation();
    if (!autoInfo.padded) return;
    node.params.extent_user_overridden = false;
    vals = autoInfo.padded;
    inputs.forEach((inp, i) => { inp.value = vals[i]; });
    save(false);
    renderInspector();
  };

  manualBtn.onclick = (event) => {
    event.preventDefault();
    event.stopPropagation();
    node.params.extent_user_overridden = true;
    save(true);
    renderInspector();
  };

  save(false);
  return box;
}


function renderOptionalShapeControl(node, prm, current, checkboxLabel) {
  const existing = parseJsonArray(current, [], 3);
  const enabled = existing.length === 3;
  const vals = enabled ? existing : [64, 64, 64];
  const box = document.createElement('div');
  box.className = 'optional-shape';
  const checkWrap = document.createElement('label');
  checkWrap.className = 'inline-check';
  const checkbox = document.createElement('input');
  checkbox.type = 'checkbox';
  checkbox.checked = enabled;
  checkWrap.appendChild(checkbox);
  checkWrap.appendChild(document.createTextNode(` ${checkboxLabel}`));
  box.appendChild(checkWrap);
  const grid = document.createElement('div');
  grid.className = 'grid-param shape-grid';
  const names = ['nx', 'ny', 'nz'];
  const inputs = [];
  names.forEach((name, i) => {
    const item = document.createElement('div');
    item.innerHTML = `<span>${name}</span>`;
    const inp = document.createElement('input');
    inp.type = 'number';
    inp.step = '1';
    inp.min = '1';
    inp.value = vals[i] ?? 64;
    inp.disabled = !checkbox.checked;
    item.appendChild(inp);
    grid.appendChild(item);
    inputs.push(inp);
  });
  box.appendChild(grid);
  const save = () => {
    for (const inp of inputs) inp.disabled = !checkbox.checked;
    node.params[prm.name] = checkbox.checked ? JSON.stringify(inputs.map(inp => Number(inp.value))) : '';
  };
  checkbox.onchange = save;
  inputs.forEach(inp => { inp.oninput = save; inp.onchange = save; });
  save();
  return box;
}


function uniqueNonEmpty(values) {
  const out = [];
  for (const v of values || []) {
    const s = String(v ?? '').trim();
    if (s && !out.includes(s)) out.push(s);
  }
  return out;
}

function outputDescriptor(nodeId, portName) {
  const res = state.nodeResults?.[nodeId];
  if (!res || !res.outputs) return null;
  return res.outputs[portName] || null;
}

function connectedOutputDescriptor(node, inputPortName) {
  if (!node) return null;
  const edge = state.edges.find(e => e.to_node === node.id && e.to_port === inputPortName);
  if (!edge) return null;
  return outputDescriptor(edge.from_node, edge.from_port);
}

function elementsFromPreview(preview) {
  if (!preview || typeof preview !== 'object') return [];
  const values = [];

  for (const key of ['available_elements_from_geo_model', 'available_elements', 'elements', 'formations']) {
    if (Array.isArray(preview[key])) values.push(...preview[key]);
  }

  if (preview.suggested_stack_mapping && typeof preview.suggested_stack_mapping === 'object') {
    for (const elems of Object.values(preview.suggested_stack_mapping)) {
      if (Array.isArray(elems)) values.push(...elems);
      else values.push(elems);
    }
  }

  if (Array.isArray(preview.structural_group_summary)) {
    preview.structural_group_summary.forEach(g => {
      if (Array.isArray(g.elements)) values.push(...g.elements);
      if (g.name && !String(g.name).startsWith('default')) values.push(g.name);
    });
  }

  for (const schemaKey of ['surface_schema', 'orientation_schema']) {
    const schema = preview[schemaKey];
    if (schema && schema.formation_counts && typeof schema.formation_counts === 'object') {
      values.push(...Object.keys(schema.formation_counts));
    }
  }

  if (Array.isArray(preview.groups_applied)) {
    preview.groups_applied.forEach(g => {
      if (Array.isArray(g.elements)) values.push(...g.elements);
    });
  }

  if (Array.isArray(preview.auto_groups)) {
    preview.auto_groups.forEach(g => {
      if (Array.isArray(g.elements)) values.push(...g.elements);
    });
  }

  return uniqueNonEmpty(values).filter(v => v !== 'default_formation');
}

function defaultSeriesNameForElement(elementName) {
  const raw = String(elementName || '').trim();
  if (!raw) return 'Series';
  const lower = raw.toLowerCase();
  if (lower.includes('fault') || lower.startsWith('f')) return `${raw}_series`;
  if (lower.includes('basement') || lower === 'base' || lower.includes('crystalline')) return 'Basement_series';
  return `${raw}_series`;
}

function mappingFromElements(elements) {
  const mapping = {};
  const used = new Set();
  uniqueNonEmpty(elements).forEach(el => {
    let base = defaultSeriesNameForElement(el);
    let name = base;
    let i = 2;
    while (used.has(name)) {
      name = `${base}_${i}`;
      i += 1;
    }
    used.add(name);
    mapping[name] = [el];
  });
  return mapping;
}

function suggestedMappingFromPreview(preview) {
  if (!preview || typeof preview !== 'object') return {};
  if (preview.suggested_stack_mapping && typeof preview.suggested_stack_mapping === 'object') {
    return preview.suggested_stack_mapping;
  }
  if (preview.auto_mapping && typeof preview.auto_mapping === 'object') {
    return preview.auto_mapping;
  }
  const elements = elementsFromPreview(preview);
  return mappingFromElements(elements);
}

function connectedGeoModelInfo(node) {
  const previews = [];

  // Prefer the incoming geo_model port.
  const incoming = connectedOutputDescriptor(node, 'geo_model');
  if (incoming && incoming.preview) previews.push(incoming.preview);

  // If this Configure node has already been run, also use its own result.
  const own = outputDescriptor(node?.id, 'geo_model');
  if (own && own.preview) previews.push(own.preview);

  // Last fallback: any upstream geo_model descriptor.
  const upstreamIds = upstreamNodeIds(node?.id || '').filter(id => id !== node?.id);
  for (const id of upstreamIds) {
    const res = state.nodeResults?.[id];
    if (!res || !res.outputs) continue;
    for (const desc of Object.values(res.outputs)) {
      if (desc && desc.kind === 'geo_model' && desc.preview) previews.push(desc.preview);
    }
  }

  const elements = uniqueNonEmpty(previews.flatMap(p => elementsFromPreview(p)));
  let mapping = {};
  for (const p of previews) {
    const m = suggestedMappingFromPreview(p);
    if (m && Object.keys(m).length) {
      mapping = m;
      break;
    }
  }
  if (!Object.keys(mapping).length && elements.length) {
    mapping = mappingFromElements(elements);
  }

  return {
    previews,
    elements,
    mapping,
    hasGeoModelResult: previews.length > 0,
  };
}

function mappingObjectIsEffectivelyEmpty(obj) {
  if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return true;
  const keys = Object.keys(obj).map(k => String(k).trim()).filter(Boolean);
  if (!keys.length) return true;
  return keys.every(k => splitCsv(Array.isArray(obj[k]) ? obj[k].join(',') : obj[k]).length === 0);
}

function mappingRowsFromObject(obj) {
  const rows = [];
  for (const [seriesRaw, elementsRaw] of Object.entries(obj || {})) {
    const series = String(seriesRaw || '').trim();
    if (!series) continue;

    const elems = Array.isArray(elementsRaw)
      ? elementsRaw.flatMap(v => splitCsv(v))
      : splitCsv(elementsRaw);

    if (elems.length) {
      // UI convention: one row = one element.  Repeated series names are valid
      // and will be grouped again when saved for GemPy.
      elems.forEach(el => rows.push({ series, elements: el }));
    } else {
      rows.push({ series, elements: '' });
    }
  }
  return rows;
}

function groupsFromMappingObject(mappingObj, existingGroups=null) {
  const byName = new Map();
  if (Array.isArray(existingGroups)) {
    existingGroups.forEach(g => {
      const name = String(g.name || '').trim();
      if (name) byName.set(name, g);
    });
  }
  return Object.keys(mappingObj || {}).map((name, idx) => {
    const elems = Array.isArray(mappingObj[name]) ? mappingObj[name] : splitCsv(mappingObj[name]);
    const lower = `${name} ${elems.join(',')}`.toLowerCase();
    let relation = String((byName.get(name) || {}).relation || '').toUpperCase();
    if (!relation) {
      if (lower.includes('fault')) relation = 'FAULT';
      else if (lower.includes('basement') || lower.includes('base') || lower.includes('crystalline')) relation = 'BASEMENT';
      else relation = 'ERODE';
    }
    return { index: idx, name, relation };
  });
}


function mappingObjectFromRows(rows) {
  const out = {};
  rows.forEach(row => {
    const series = String(row.series || '').trim();
    if (!series) return;

    // Each UI row may contain one element, but comma-separated values are still
    // accepted for convenience.  If multiple rows use the same series name,
    // accumulate their elements instead of overwriting earlier rows.
    const elems = splitCsv(row.elements);
    if (!Object.prototype.hasOwnProperty.call(out, series)) out[series] = [];

    for (const el of elems) {
      if (!out[series].includes(el)) out[series].push(el);
    }
  });

  // GemPy accepts list values.  Always use lists here so duplicate-series rows
  // are preserved unambiguously in the saved JSON.
  return out;
}

function syncStructuralGroupsWithMapping(node, mappingObj) {
  const existing = parseJsonArray(node.params.groups_json, [], null);
  const byName = new Map();
  if (Array.isArray(existing)) {
    for (const g of existing) {
      const name = String(g.name || '').trim();
      if (name) byName.set(name, g);
    }
  }
  const out = Object.keys(mappingObj).map((name, idx) => ({
    index: idx,
    name,
    relation: String((byName.get(name) || {}).relation || 'ERODE').toUpperCase(),
  }));
  // Keep manually-created extra groups only if they are not represented in the stack mapping
  // and still have explicit elements. This is useful for optional fault-only groups.
  if (Array.isArray(existing)) {
    for (const g of existing) {
      const name = String(g.name || '').trim();
      if (!name || Object.prototype.hasOwnProperty.call(mappingObj, name)) continue;
      const elements = Array.isArray(g.elements) ? g.elements : splitCsv(g.elements);
      if (elements.length) {
        out.push({
          index: out.length,
          name,
          elements,
          relation: String(g.relation || 'ERODE').toUpperCase(),
        });
      }
    }
  }
  node.params.groups_json = JSON.stringify(out, null, 2);
}

function renderMappingBuilder(node, prm, current) {
  const geoInfo = connectedGeoModelInfo(node);
  let obj = parseJsonObject(current, {});
  const autoEnabled =
    node.params.auto_mapping_from_geo_model !== false &&
    String(node.params.auto_mapping_from_geo_model ?? 'true').toLowerCase() !== 'false';
  const userOverridden =
    node.params.mapping_user_overridden === true ||
    String(node.params.mapping_user_overridden ?? 'false').toLowerCase() === 'true';

  // Auto-fill is only an initialization step. Once the user edits/clears/sorts,
  // mapping_user_overridden becomes true and auto-fill will no longer overwrite
  // manual edits when the node is reselected or rerun.
  const autoInitialized =
    node.params.mapping_auto_initialized === true ||
    String(node.params.mapping_auto_initialized ?? 'false').toLowerCase() === 'true';

  if (autoEnabled && !autoInitialized && !userOverridden && mappingObjectIsEffectivelyEmpty(obj) && Object.keys(geoInfo.mapping).length) {
    obj = geoInfo.mapping;
    node.params[prm.name] = JSON.stringify(obj, null, 2);
    node.params.mapping_auto_initialized = true;

    const existing = parseJsonArray(node.params.groups_json || '[]', [], null);
    const groupsUserOverridden =
      node.params.groups_user_overridden === true ||
      String(node.params.groups_user_overridden ?? 'false').toLowerCase() === 'true';
    if (!groupsUserOverridden && (!Array.isArray(existing) || !existing.some(g => String(g.name || '').trim()))) {
      node.params.groups_json = JSON.stringify(groupsFromMappingObject(obj, existing), null, 2);
    }
  }

  let rows = mappingRowsFromObject(obj);

  const box = document.createElement('div');
  box.className = 'builder mapping-builder';

  const hint = document.createElement('div');
  hint.className = 'param-help inline-note';
  if (userOverridden) {
    hint.textContent = 'Manual mapping override is active. Your edited mapping will not be replaced automatically. Use “Fill from connected GeoModel” to intentionally reset it.';
  } else if (autoInitialized) {
    hint.textContent = `Mapping was auto-filled once from the connected GeoModel and is now locked. Use one row per element; repeated series names are allowed and will be grouped for GemPy.`;
  } else if (geoInfo.hasGeoModelResult) {
    hint.textContent = `Detected ${geoInfo.elements.length} element/formation name(s) from the connected GeoModel. Edit rows to override the automatic mapping.`;
  } else {
    hint.textContent = 'Run the connected Create GemPy Model node first, then this editor can auto-fill mapping from that GeoModel. You can also add rows manually.';
  }
  box.appendChild(hint);

  const tools = document.createElement('div');
  tools.className = 'builder-actions mapping-actions';
  const refreshBtn = document.createElement('button');
  refreshBtn.type = 'button';
  refreshBtn.textContent = '↻ Fill from connected GeoModel';
  refreshBtn.disabled = !Object.keys(geoInfo.mapping).length;
  const clearBtn = document.createElement('button');
  clearBtn.type = 'button';
  clearBtn.textContent = 'Clear mapping';
  clearBtn.className = 'danger';
  tools.appendChild(refreshBtn);
  tools.appendChild(clearBtn);
  box.appendChild(tools);

  if (geoInfo.elements.length) {
    const chips = document.createElement('div');
    chips.className = 'detected-elements';
    geoInfo.elements.forEach(name => {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'element-chip';
      chip.textContent = name;
      chip.title = 'Click to add this element as a new mapping row';
      chip.onclick = (event) => {
        event.preventDefault();
        event.stopPropagation();
        node.params.mapping_user_overridden = true;
        node.params.mapping_auto_initialized = true;
        rows.push({ series: defaultSeriesNameForElement(name), elements: name });
        renderRows();
        save();
        renderInspector();
      };
      chips.appendChild(chip);
    });
    box.appendChild(chips);
  }

  const list = document.createElement('div');
  list.className = 'builder-list';
  box.appendChild(list);

  const addBtn = document.createElement('button');
  addBtn.type = 'button';
  addBtn.textContent = '+ Add mapping';
  box.appendChild(addBtn);

  const save = () => {
    const out = mappingObjectFromRows(rows);
    node.params[prm.name] = Object.keys(out).length ? JSON.stringify(out, null, 2) : '';
    syncStructuralGroupsWithMapping(node, out);
  };

  const markManualAndSave = () => {
    node.params.mapping_user_overridden = true;
    node.params.mapping_auto_initialized = true;
    save();
  };

  const renderRows = () => {
    list.innerHTML = '';
    rows.forEach((row, idx) => {
      const div = document.createElement('div');
      div.className = 'builder-row mapping-row';

      const series = document.createElement('input');
      series.type = 'text';
      series.placeholder = 'Series name';
      series.value = row.series || '';

      const elements = document.createElement('input');
      elements.type = 'text';
      elements.placeholder = 'Element name (one row per element)';
      elements.setAttribute('list', 'formationOptions');
      elements.value = row.elements || '';

      const up = document.createElement('button');
      up.type = 'button';
      up.className = 'mini';
      up.textContent = '↑';
      up.disabled = idx === 0;

      const down = document.createElement('button');
      down.type = 'button';
      down.className = 'mini';
      down.textContent = '↓';
      down.disabled = idx === rows.length - 1;

      const del = document.createElement('button');
      del.type = 'button';
      del.className = 'danger mini';
      del.textContent = '×';

      series.oninput = () => {
        row.series = series.value;
        markManualAndSave();
      };
      elements.oninput = () => {
        row.elements = elements.value;
        markManualAndSave();
      };
      up.onclick = () => {
        node.params.mapping_user_overridden = true;
        [rows[idx - 1], rows[idx]] = [rows[idx], rows[idx - 1]];
        renderRows();
        save();
        renderInspector();
      };
      down.onclick = () => {
        node.params.mapping_user_overridden = true;
        [rows[idx + 1], rows[idx]] = [rows[idx], rows[idx + 1]];
        renderRows();
        save();
        renderInspector();
      };
      del.onclick = () => {
        node.params.mapping_user_overridden = true;
        rows.splice(idx, 1);
        renderRows();
        save();
        renderInspector();
      };

      div.appendChild(series);
      div.appendChild(elements);
      div.appendChild(up);
      div.appendChild(down);
      div.appendChild(del);
      list.appendChild(div);
    });

    appendFormationDatalist(box, geoInfo.elements);
  };

  refreshBtn.onclick = (event) => {
    event.preventDefault();
    event.stopPropagation();
    if (!Object.keys(geoInfo.mapping).length) return;
    rows = mappingRowsFromObject(geoInfo.mapping);
    node.params.mapping_user_overridden = false;
    node.params.mapping_auto_initialized = true;
    renderRows();
    save();
    renderInspector();
  };

  clearBtn.onclick = (event) => {
    event.preventDefault();
    event.stopPropagation();
    rows = [];
    renderRows();
    node.params.mapping_user_overridden = true;
    node.params.mapping_auto_initialized = true;
    node.params[prm.name] = '';
    node.params.groups_json = '[]';
    node.params.groups_user_overridden = true;
    renderInspector();
  };

  addBtn.onclick = (event) => {
    event.preventDefault();
    event.stopPropagation();
    node.params.mapping_user_overridden = true;
    node.params.mapping_auto_initialized = true;
    rows.push({ series: '', elements: '' });
    renderRows();
  };

  renderRows();
  save();
  return box;
}


function renderStructuralGroupsBuilder(node, prm, current) {
  const mappingObj = parseJsonObject(node.params.mapping_json || '', {});
  const mappingNames = Object.keys(mappingObj);
  let rows = parseJsonArray(current, [], null);
  if (!Array.isArray(rows)) rows = [];

  const box = document.createElement('div');
  box.className = 'builder groups-builder';
  const list = document.createElement('div');
  list.className = 'builder-list';
  box.appendChild(list);

  if (mappingNames.length) {
    const byName = new Map();
    rows.forEach(g => {
      const name = String(g.name || '').trim();
      if (name) byName.set(name, g);
    });
    rows = mappingNames.map((name, idx) => ({
      index: idx,
      name,
      relation: String((byName.get(name) || {}).relation || 'ERODE').toUpperCase(),
    }));

    const note = document.createElement('div');
    note.className = 'param-help inline-note';
    note.textContent = 'Stack mapping is active: order and elements come from Stack mapping. Here you only set the relation for each mapped series.';
    box.insertBefore(note, list);

    const save = () => {
      const out = rows.map((row, idx) => ({
        index: idx,
        name: row.name,
        relation: row.relation || 'ERODE',
      }));
      node.params[prm.name] = JSON.stringify(out, null, 2);
    };

    const renderRows = () => {
      list.innerHTML = '';
      rows.forEach((row, idx) => {
        const card = document.createElement('div');
        card.className = 'group-card relation-only-card';
        const top = document.createElement('div');
        top.className = 'group-card-top';
        top.innerHTML = `<b>#${idx}</b><span class="muted"> order from Stack mapping</span>`;
        card.appendChild(top);

        const name = document.createElement('input');
        name.type = 'text';
        name.value = row.name;
        name.readOnly = true;
        const relation = document.createElement('select');
        for (const r of STRUCTURAL_RELATIONS) {
          const opt = document.createElement('option');
          opt.value = r;
          opt.textContent = r;
          if (r === row.relation) opt.selected = true;
          relation.appendChild(opt);
        }
        const grid = document.createElement('div');
        grid.className = 'group-fields';
        grid.appendChild(fieldWithLabel('Series', name));
        grid.appendChild(fieldWithLabel('Relation', relation));
        card.appendChild(grid);
        relation.onchange = () => { row.relation = relation.value; save(); };
        list.appendChild(card);
      });
    };
    renderRows();
    save();
    return box;
  }

  rows = rows.map((g, idx) => ({
    index: idx,
    name: g.name || '',
    elements: Array.isArray(g.elements) ? g.elements.join(', ') : String(g.elements || ''),
    relation: String(g.relation || 'ERODE').toUpperCase(),
  }));

  const actions = document.createElement('div');
  actions.className = 'builder-actions';
  const addBtn = document.createElement('button');
  addBtn.type = 'button';
  addBtn.textContent = '+ Add group';
  actions.appendChild(addBtn);
  box.appendChild(actions);

  const save = () => {
    const out = rows.map((row, idx) => ({
      index: idx,
      name: String(row.name || '').trim(),
      elements: splitCsv(row.elements),
      relation: row.relation || 'ERODE',
    })).filter(row => row.name && row.elements.length);
    node.params[prm.name] = JSON.stringify(out, null, 2);
  };

  const renderRows = () => {
    list.innerHTML = '';
    rows.forEach((row, idx) => {
      const card = document.createElement('div');
      card.className = 'group-card';
      const top = document.createElement('div');
      top.className = 'group-card-top';
      top.innerHTML = `<b>#${idx}</b>`;
      const up = document.createElement('button');
      up.type = 'button';
      up.className = 'mini';
      up.textContent = '↑';
      up.disabled = idx === 0;
      const down = document.createElement('button');
      down.type = 'button';
      down.className = 'mini';
      down.textContent = '↓';
      down.disabled = idx === rows.length - 1;
      const del = document.createElement('button');
      del.type = 'button';
      del.className = 'danger mini';
      del.textContent = 'Delete';
      top.appendChild(up);
      top.appendChild(down);
      top.appendChild(del);
      card.appendChild(top);

      const name = document.createElement('input');
      name.type = 'text';
      name.placeholder = 'Group name, e.g. Fault_series';
      name.value = row.name;
      const relation = document.createElement('select');
      for (const r of STRUCTURAL_RELATIONS) {
        const opt = document.createElement('option');
        opt.value = r;
        opt.textContent = r;
        if (r === row.relation) opt.selected = true;
        relation.appendChild(opt);
      }
      const elements = document.createElement('input');
      elements.type = 'text';
      elements.placeholder = 'Element name (one row per element)';
      elements.value = row.elements;
      elements.setAttribute('list', 'formationOptions');
      const grid = document.createElement('div');
      grid.className = 'group-fields';
      grid.appendChild(fieldWithLabel('Name', name));
      grid.appendChild(fieldWithLabel('Relation', relation));
      grid.appendChild(fieldWithLabel('Elements', elements));
      card.appendChild(grid);

      name.oninput = () => { row.name = name.value; save(); };
      relation.onchange = () => { row.relation = relation.value; save(); };
      elements.oninput = () => { row.elements = elements.value; save(); };
      up.onclick = () => { [rows[idx - 1], rows[idx]] = [rows[idx], rows[idx - 1]]; renderRows(); save(); };
      down.onclick = () => { [rows[idx + 1], rows[idx]] = [rows[idx], rows[idx + 1]]; renderRows(); save(); };
      del.onclick = () => { rows.splice(idx, 1); renderRows(); save(); };
      list.appendChild(card);
    });
    appendFormationDatalist(box);
  };
  addBtn.onclick = () => { rows.push({ name: '', elements: '', relation: 'ERODE' }); renderRows(); save(); };
  renderRows();
  save();
  return box;
}



function getStructuralSeriesRows(node) {
  const mappingObj = parseJsonObject(node.params.mapping_json || '', {});
  const mappingNames = Object.keys(mappingObj);
  if (mappingNames.length) {
    const groups = parseJsonArray(node.params.groups_json || '[]', [], null);
    const relationByName = new Map();
    if (Array.isArray(groups)) {
      groups.forEach(g => {
        if (g && g.name) relationByName.set(String(g.name), String(g.relation || 'ERODE').toUpperCase());
      });
    }
    return mappingNames.map((name, idx) => ({
      index: idx,
      name,
      relation: relationByName.get(name) || 'ERODE',
      elements: Array.isArray(mappingObj[name]) ? mappingObj[name] : splitCsv(mappingObj[name]),
    }));
  }
  const groups = parseJsonArray(node.params.groups_json || '[]', [], null);
  if (Array.isArray(groups)) {
    return groups
      .filter(g => g && String(g.name || '').trim())
      .map((g, idx) => ({
        index: idx,
        name: String(g.name || '').trim(),
        relation: String(g.relation || 'ERODE').toUpperCase(),
        elements: Array.isArray(g.elements) ? g.elements : splitCsv(g.elements),
      }));
  }
  return [];
}


function getStructuralGroupsForFiniteFaultNode(node) {
  // First use upstream Configure Structural Frame nodes.
  const upstream = upstreamNodeIds(node.id)
    .map(id => state.nodes.find(n => n.id === id))
    .filter(Boolean);

  const cfgNodes = upstream.filter(n => n.type === 'ConfigureStructuralFrame');
  cfgNodes.sort((a, b) => {
    const ax = a.position?.x || 0;
    const bx = b.position?.x || 0;
    const nx = node.position?.x || 0;
    return Math.abs(ax - nx) - Math.abs(bx - nx);
  });

  for (const cfg of cfgNodes) {
    const groups = getStructuralSeriesRows(cfg);
    if (groups.length) return groups;
  }

  // Fallback to any Configure Structural Frame node on the canvas.
  for (const cfg of state.nodes.filter(n => n.type === 'ConfigureStructuralFrame')) {
    const groups = getStructuralSeriesRows(cfg);
    if (groups.length) return groups;
  }

  return [];
}

function parseVec3ForEditor(value, fallback=[0, 0, 0]) {
  if (Array.isArray(value)) {
    const arr = value.map(v => Number(v));
    return [0, 1, 2].map(i => Number.isFinite(arr[i]) ? arr[i] : fallback[i]);
  }
  const raw = String(value ?? '').trim();
  if (!raw) return [...fallback];
  const parts = raw.split(',').map(v => Number(String(v).trim()));
  return [0, 1, 2].map(i => Number.isFinite(parts[i]) ? parts[i] : fallback[i]);
}

function vec3Inputs(values) {
  const wrap = document.createElement('div');
  wrap.className = 'vec3-inputs';
  const labels = ['X', 'Y', 'Z'];
  const inputs = labels.map((lab, i) => {
    const inp = document.createElement('input');
    inp.type = 'number';
    inp.step = 'any';
    inp.placeholder = lab;
    inp.value = values[i] ?? 0;
    wrap.appendChild(inp);
    return inp;
  });
  return { wrap, inputs };
}

function readVec3(inputs) {
  return inputs.map(inp => Number(inp.value || 0));
}

function groupSelectForFiniteFault(value, groups) {
  const sel = document.createElement('select');
  const empty = document.createElement('option');
  empty.value = '';
  empty.textContent = '— choose fault group —';
  sel.appendChild(empty);

  const faultGroups = groups.filter(g => String(g.relation || '').toUpperCase() === 'FAULT');
  const shown = faultGroups.length ? faultGroups : groups;

  for (const g of shown) {
    const opt = document.createElement('option');
    opt.value = g.name;
    opt.textContent = `${g.name}${g.relation ? ' [' + g.relation + ']' : ''}`;
    opt.title = (g.elements || []).join(', ');
    if (g.name === value) opt.selected = true;
    sel.appendChild(opt);
  }

  if (value && !shown.some(g => g.name === value)) {
    const opt = document.createElement('option');
    opt.value = value;
    opt.textContent = `${value} (not found in current groups)`;
    opt.selected = true;
    sel.appendChild(opt);
  }
  return sel;
}


function renderFaultRelationsBuilder(node, prm, current) {
  let cfg = parseJsonObject(current, { enabled: false, relations: [] });
  const groups = getStructuralSeriesRows(node);
  const names = groups.map(g => g.name).filter(Boolean);
  const relationKey = (a, b) => `${a}|||${b}`;
  const active = new Set();

  if (Array.isArray(cfg.relations)) {
    cfg.relations.forEach(r => {
      const from = String(r.from || r.faulting || '').trim();
      const to = String(r.to || r.affected || '').trim();
      if (from && to && String(r.active ?? true).toLowerCase() !== 'false') {
        active.add(relationKey(from, to));
      }
    });
  }

  const box = document.createElement('div');
  box.className = 'builder fault-relations-builder fault-relations-list-ui';

  const enabledWrap = document.createElement('label');
  enabledWrap.className = 'checkbox-line';
  const enabled = document.createElement('input');
  enabled.type = 'checkbox';
  enabled.checked = !!cfg.enabled && String(cfg.enabled).toLowerCase() !== 'false';
  enabledWrap.appendChild(enabled);
  enabledWrap.appendChild(document.createTextNode(' Apply geo_model.structural_frame.fault_relations'));
  box.appendChild(enabledWrap);

  const note = document.createElement('div');
  note.className = 'param-help inline-note';
  note.textContent = 'Rows are faulting series. Tick the affected series below. This compact layout avoids horizontal overflow.';
  box.appendChild(note);

  const actions = document.createElement('div');
  actions.className = 'builder-actions fault-actions';
  const autoBtn = document.createElement('button');
  autoBtn.type = 'button';
  autoBtn.textContent = 'Fault rows affect following groups';
  const clearBtn = document.createElement('button');
  clearBtn.type = 'button';
  clearBtn.textContent = 'Clear';
  clearBtn.className = 'danger';
  actions.appendChild(autoBtn);
  actions.appendChild(clearBtn);
  box.appendChild(actions);

  const list = document.createElement('div');
  list.className = 'fault-relation-list';
  box.appendChild(list);

  const save = () => {
    const relations = [];
    for (const key of active) {
      const [from, to] = key.split('|||');
      if (from && to && from !== to && names.includes(from) && names.includes(to)) {
        relations.push({ from, to, active: true });
      }
    }
    node.params[prm.name] = JSON.stringify({ enabled: enabled.checked, relations }, null, 2);
  };

  const renderList = () => {
    list.innerHTML = '';

    if (!names.length) {
      const empty = document.createElement('div');
      empty.className = 'param-help warning-note';
      empty.textContent = 'Define Stack mapping rows first. Then fault relation checkboxes will appear here.';
      list.appendChild(empty);
      save();
      return;
    }

    groups.forEach((group, i) => {
      const card = document.createElement('div');
      card.className = 'fault-relation-card';

      const header = document.createElement('div');
      header.className = 'fault-relation-card-header';
      const rel = String(group.relation || '').toUpperCase();
      header.innerHTML = `<b>${escapeHtml(group.name)}</b><span>${escapeHtml(rel || 'relation not set')}</span>`;
      card.appendChild(header);

      const chips = document.createElement('div');
      chips.className = 'fault-affected-chips';

      names.forEach((affected, j) => {
        if (i === j) return;

        const label = document.createElement('label');
        label.className = 'fault-chip';
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        const key = relationKey(group.name, affected);
        cb.checked = active.has(key);
        cb.onchange = () => {
          if (cb.checked) active.add(key);
          else active.delete(key);
          save();
        };
        label.appendChild(cb);
        label.appendChild(document.createTextNode(affected));
        chips.appendChild(label);
      });

      card.appendChild(chips);
      list.appendChild(card);
    });

    save();
  };

  enabled.onchange = save;

  autoBtn.onclick = (event) => {
    event.preventDefault();
    event.stopPropagation();
    active.clear();
    groups.forEach((g, i) => {
      if (String(g.relation || '').toUpperCase() === 'FAULT') {
        for (let j = i + 1; j < groups.length; j++) {
          active.add(relationKey(g.name, groups[j].name));
        }
      }
    });
    enabled.checked = true;
    renderList();
    save();
  };

  clearBtn.onclick = (event) => {
    event.preventDefault();
    event.stopPropagation();
    active.clear();
    renderList();
    save();
  };

  renderList();
  return box;
}


function renderFiniteFaultBuilder(node, prm, current) {
  let rows = parseJsonArray(current, [], null);
  if (!Array.isArray(rows)) rows = [];

  const groups = getStructuralGroupsForFiniteFaultNode(node);
  const faultGroups = groups.filter(g => String(g.relation || '').toUpperCase() === 'FAULT');

  rows = rows.map(r => ({
    enabled: r.enabled !== false && String(r.enabled ?? true).toLowerCase() !== 'false',
    group_name: r.group_name || '',
    group_index: r.group_index ?? '',
    center: parseVec3ForEditor(r.center, [0, 0, 0]),
    radius: parseVec3ForEditor(r.radius, [1000, 1000, 100]),
    max_slope: parseVec3ForEditor(r.max_slope, [1, 1, 1]),
    transform_position: parseVec3ForEditor(r.transform_position, [0, 0, 0]),
    transform_rotation: parseVec3ForEditor(r.transform_rotation, [0, 0, 0]),
    transform_scale: parseVec3ForEditor(r.transform_scale, [1, 1, 1]),
  }));

  const box = document.createElement('div');
  box.className = 'builder finite-fault-builder';

  const note = document.createElement('div');
  note.className = 'param-help inline-note';
  note.textContent = 'Finite faults use GemPy ellipsoid_3d_factory. Select an upstream FAULT group, then set ellipsoid parameters.';
  box.appendChild(note);

  if (!groups.length) {
    const warn = document.createElement('div');
    warn.className = 'param-help warning-note';
    warn.textContent = 'No structural groups found. Connect this node after Configure Structural Frame.';
    box.appendChild(warn);
  }

  const list = document.createElement('div');
  list.className = 'builder-list';
  box.appendChild(list);

  const actions = document.createElement('div');
  actions.className = 'builder-actions';
  const addBtn = document.createElement('button');
  addBtn.type = 'button';
  addBtn.textContent = '+ Add finite fault';
  actions.appendChild(addBtn);
  box.appendChild(actions);

  const save = () => {
    const out = rows.map(row => ({
      enabled: !!row.enabled,
      group_name: String(row.group_name || '').trim(),
      group_index: row.group_index === '' ? '' : Number(row.group_index),
      center: row.center.join(','),
      radius: row.radius.join(','),
      max_slope: row.max_slope.join(','),
      transform_position: row.transform_position.join(','),
      transform_rotation: row.transform_rotation.join(','),
      transform_scale: row.transform_scale.join(','),
    })).filter(row => row.group_name || row.group_index !== '');
    node.params[prm.name] = JSON.stringify(out, null, 2);
  };

  const renderRows = () => {
    list.innerHTML = '';
    rows.forEach((row, idx) => {
      const card = document.createElement('div');
      card.className = 'group-card finite-fault-card';

      const top = document.createElement('div');
      top.className = 'group-card-top';
      top.innerHTML = `<b>#${idx} finite fault</b>`;

      const enabled = document.createElement('input');
      enabled.type = 'checkbox';
      enabled.checked = row.enabled;

      const del = document.createElement('button');
      del.type = 'button';
      del.className = 'danger mini';
      del.textContent = 'Delete';

      top.appendChild(fieldWithLabel('enabled', enabled));
      top.appendChild(del);
      card.appendChild(top);

      const grid = document.createElement('div');
      grid.className = 'finite-fault-fields';

      const groupName = groupSelectForFiniteFault(row.group_name, groups);
      const groupIndex = numberInput(row.group_index, '0');
      groupIndex.step = '1';

      const center = vec3Inputs(row.center);
      const radius = vec3Inputs(row.radius);
      const maxSlope = vec3Inputs(row.max_slope);
      const pos = vec3Inputs(row.transform_position);
      const rot = vec3Inputs(row.transform_rotation);
      const scale = vec3Inputs(row.transform_scale);

      grid.appendChild(fieldWithLabel('Fault group', groupName));
      grid.appendChild(fieldWithLabel('Group index fallback', groupIndex));
      grid.appendChild(fieldWithLabel('Center X/Y/Z', center.wrap));
      grid.appendChild(fieldWithLabel('Radius X/Y/Z', radius.wrap));
      grid.appendChild(fieldWithLabel('Max slope X/Y/Z', maxSlope.wrap));
      grid.appendChild(fieldWithLabel('Transform position X/Y/Z', pos.wrap));
      grid.appendChild(fieldWithLabel('Transform rotation X/Y/Z', rot.wrap));
      grid.appendChild(fieldWithLabel('Transform scale X/Y/Z', scale.wrap));
      card.appendChild(grid);

      const updateVecs = () => {
        row.center = readVec3(center.inputs);
        row.radius = readVec3(radius.inputs);
        row.max_slope = readVec3(maxSlope.inputs);
        row.transform_position = readVec3(pos.inputs);
        row.transform_rotation = readVec3(rot.inputs);
        row.transform_scale = readVec3(scale.inputs);
        save();
      };

      enabled.onchange = () => { row.enabled = enabled.checked; save(); };
      groupName.onchange = () => { row.group_name = groupName.value; save(); };
      groupIndex.oninput = () => { row.group_index = groupIndex.value; save(); };
      [...center.inputs, ...radius.inputs, ...maxSlope.inputs, ...pos.inputs, ...rot.inputs, ...scale.inputs].forEach(inp => {
        inp.oninput = updateVecs;
        inp.onchange = updateVecs;
      });
      del.onclick = () => { rows.splice(idx, 1); renderRows(); save(); };
      list.appendChild(card);
    });
  };

  addBtn.onclick = () => {
    const defaultGroup = (faultGroups[0] || groups[0] || {}).name || '';
    rows.push({
      enabled: true,
      group_name: defaultGroup,
      group_index: '',
      center: [0, 0, 0],
      radius: [1000, 1000, 100],
      max_slope: [1, 1, 1],
      transform_position: [0, 0, 0],
      transform_rotation: [0, 0, 0],
      transform_scale: [1, 1, 1],
    });
    renderRows();
    save();
  };

  renderRows();
  save();
  return box;
}


function appendGroupDatalist(container, groups) {
  if (document.getElementById('groupOptions')) return;
  const dl = document.createElement('datalist');
  dl.id = 'groupOptions';
  for (const g of groups || []) {
    const opt = document.createElement('option');
    opt.value = g.name;
    dl.appendChild(opt);
  }
  document.body.appendChild(dl);
}


function renderSurfacePointsBuilder(node, prm, current) {
  let rows = parseJsonArray(current, [], null);
  if (!Array.isArray(rows)) rows = [];
  rows = rows.map(r => ({
    mode: String(r.mode || 'single').toLowerCase() === 'grid' ? 'grid' : 'single',
    element: r.element || '',
    x: r.x ?? '',
    y: r.y ?? '',
    z: r.z ?? '',
    xs: r.xs || '',
    ys: r.ys || '',
  }));

  const box = document.createElement('div');
  box.className = 'builder surface-points-builder';
  const list = document.createElement('div');
  list.className = 'builder-list';
  box.appendChild(list);

  const actions = document.createElement('div');
  actions.className = 'builder-actions';
  const addSingle = document.createElement('button');
  addSingle.type = 'button';
  addSingle.textContent = '+ Single point';
  const addGrid = document.createElement('button');
  addGrid.type = 'button';
  addGrid.textContent = '+ X/Y grid';
  actions.appendChild(addSingle);
  actions.appendChild(addGrid);
  box.appendChild(actions);

  const save = () => {
    const out = rows.map(row => {
      if (row.mode === 'grid') {
        return {
          mode: 'grid',
          element: String(row.element || '').trim(),
          xs: String(row.xs || '').trim(),
          ys: String(row.ys || '').trim(),
          z: row.z === '' ? '' : Number(row.z),
        };
      }
      return {
        mode: 'single',
        element: String(row.element || '').trim(),
        x: row.x === '' ? '' : Number(row.x),
        y: row.y === '' ? '' : Number(row.y),
        z: row.z === '' ? '' : Number(row.z),
      };
    }).filter(row => row.element);
    node.params[prm.name] = JSON.stringify(out, null, 2);
  };

  const renderRows = () => {
    list.innerHTML = '';
    rows.forEach((row, idx) => {
      const card = document.createElement('div');
      card.className = 'group-card point-card';
      const top = document.createElement('div');
      top.className = 'group-card-top';
      top.innerHTML = `<b>#${idx} ${row.mode === 'grid' ? 'Grid' : 'Single'}</b>`;
      const mode = document.createElement('select');
      for (const optName of ['single', 'grid']) {
        const opt = document.createElement('option');
        opt.value = optName;
        opt.textContent = optName === 'grid' ? 'x-y grid' : 'single point';
        if (optName === row.mode) opt.selected = true;
        mode.appendChild(opt);
      }
      const del = document.createElement('button');
      del.type = 'button';
      del.className = 'danger mini';
      del.textContent = 'Delete';
      top.appendChild(mode);
      top.appendChild(del);
      card.appendChild(top);

      const element = document.createElement('input');
      element.type = 'text';
      element.placeholder = 'Element / formation name, e.g. PraePerm';
      element.value = row.element || '';
      element.setAttribute('list', 'formationOptions');

      const grid = document.createElement('div');
      grid.className = row.mode === 'grid' ? 'point-fields grid-mode' : 'point-fields single-mode';
      grid.appendChild(fieldWithLabel('Element', element));
      if (row.mode === 'grid') {
        const xs = document.createElement('textarea');
        xs.placeholder = 'x values, comma-separated';
        xs.value = row.xs || '';
        const ys = document.createElement('textarea');
        ys.placeholder = 'y values, comma-separated';
        ys.value = row.ys || '';
        const z = numberInput(row.z, 'z');
        grid.appendChild(fieldWithLabel('X values', xs));
        grid.appendChild(fieldWithLabel('Y values', ys));
        grid.appendChild(fieldWithLabel('Z', z));
        xs.oninput = () => { row.xs = xs.value; save(); };
        ys.oninput = () => { row.ys = ys.value; save(); };
        z.oninput = () => { row.z = z.value; save(); };
        z.onchange = z.oninput;
      } else {
        const x = numberInput(row.x, 'x');
        const y = numberInput(row.y, 'y');
        const z = numberInput(row.z, 'z');
        grid.appendChild(fieldWithLabel('X', x));
        grid.appendChild(fieldWithLabel('Y', y));
        grid.appendChild(fieldWithLabel('Z', z));
        x.oninput = () => { row.x = x.value; save(); };
        y.oninput = () => { row.y = y.value; save(); };
        z.oninput = () => { row.z = z.value; save(); };
        x.onchange = x.oninput; y.onchange = y.oninput; z.onchange = z.oninput;
      }
      card.appendChild(grid);

      element.oninput = () => { row.element = element.value; save(); };
      mode.onchange = () => { row.mode = mode.value; renderRows(); save(); };
      del.onclick = () => { rows.splice(idx, 1); renderRows(); save(); };
      list.appendChild(card);
    });
    appendFormationDatalist(box);
  };
  addSingle.onclick = () => { rows.push({ mode: 'single', element: '', x: '', y: '', z: '' }); renderRows(); save(); };
  addGrid.onclick = () => { rows.push({ mode: 'grid', element: '', xs: '', ys: '', z: '' }); renderRows(); save(); };
  renderRows();
  save();
  return box;
}

function renderLayerStylesBuilder(node, prm, current) {
  let rows = parseJsonArray(current, [], null);
  if (!Array.isArray(rows)) rows = [];
  rows = rows.map(r => ({
    id: r.id ?? '',
    label: r.label || '',
    color: r.color || '#cccccc',
    opacity: r.opacity ?? 0.5,
  }));

  const box = document.createElement('div');
  box.className = 'builder layer-style-builder';
  const list = document.createElement('div');
  list.className = 'builder-list';
  box.appendChild(list);
  const actions = document.createElement('div');
  actions.className = 'builder-actions';
  const addBtn = document.createElement('button');
  addBtn.type = 'button';
  addBtn.textContent = '+ Add layer';
  actions.appendChild(addBtn);
  box.appendChild(actions);

  const save = () => {
    const out = rows.map(row => ({
      id: row.id === '' ? '' : Number(row.id),
      label: String(row.label || '').trim(),
      color: String(row.color || '').trim(),
      opacity: row.opacity === '' ? 0.5 : Number(row.opacity),
    })).filter(row => row.id !== '' && !Number.isNaN(row.id));
    node.params[prm.name] = JSON.stringify(out, null, 2);
  };

  const renderRows = () => {
    list.innerHTML = '';
    rows.forEach((row, idx) => {
      const div = document.createElement('div');
      div.className = 'builder-row layer-row';
      const id = numberInput(row.id, 'id');
      id.step = '1';
      const label = document.createElement('input');
      label.type = 'text';
      label.placeholder = 'label';
      label.value = row.label || '';
      const color = document.createElement('input');
      color.type = 'color';
      color.value = normalizeColor(row.color || '#cccccc');
      const opacity = numberInput(row.opacity, 'opacity');
      opacity.min = '0'; opacity.max = '1'; opacity.step = '0.05';
      const del = document.createElement('button');
      del.type = 'button';
      del.className = 'danger mini';
      del.textContent = '×';
      id.oninput = () => { row.id = id.value; save(); };
      label.oninput = () => { row.label = label.value; save(); };
      color.oninput = () => { row.color = color.value; save(); };
      opacity.oninput = () => { row.opacity = opacity.value; save(); };
      del.onclick = () => { rows.splice(idx, 1); renderRows(); save(); };
      div.appendChild(fieldWithLabel('id', id));
      div.appendChild(fieldWithLabel('label', label));
      div.appendChild(fieldWithLabel('color', color));
      div.appendChild(fieldWithLabel('opacity', opacity));
      div.appendChild(del);
      list.appendChild(div);
    });
  };
  addBtn.onclick = () => { rows.push({ id: '', label: '', color: '#cccccc', opacity: 0.5 }); renderRows(); save(); };
  renderRows();
  save();
  return box;
}

function numberInput(value, placeholder='') {
  const input = document.createElement('input');
  input.type = 'number';
  input.step = 'any';
  input.placeholder = placeholder;
  input.value = value ?? '';
  return input;
}

function normalizeColor(value) {
  const s = String(value || '').trim();
  return /^#[0-9a-fA-F]{6}$/.test(s) ? s : '#cccccc';
}

function fieldWithLabel(label, input) {
  const wrap = document.createElement('div');
  const span = document.createElement('span');
  span.textContent = label;
  wrap.appendChild(span);
  wrap.appendChild(input);
  return wrap;
}

function appendFormationDatalist(container, explicitOptions=null) {
  const old = document.getElementById('formationOptions');
  if (old) old.remove();

  let names = explicitOptions;
  if (!names || !names.length) {
    const node = state.nodes.find(n => n.id === state.selectedNodeId);
    if (node) {
      names = connectedGeoModelInfo(node).elements;
    }
  }
  names = uniqueNonEmpty(names || []);

  const dl = document.createElement('datalist');
  dl.id = 'formationOptions';
  for (const name of names) {
    const opt = document.createElement('option');
    opt.value = name;
    dl.appendChild(opt);
  }
  document.body.appendChild(dl);
}

function parseJsonArray(value, fallback, expectedLength) {
  try {
    const arr = Array.isArray(value) ? value : JSON.parse(value || '[]');
    if (!Array.isArray(arr)) return fallback;
    if (expectedLength && arr.length !== expectedLength) return fallback;
    return arr;
  } catch (_) {
    return fallback;
  }
}
function parseJsonObject(value, fallback) {
  try {
    const obj = typeof value === 'object' && value !== null ? value : JSON.parse(value || '{}');
    if (!obj || Array.isArray(obj) || typeof obj !== 'object') return fallback;
    return obj;
  } catch (_) {
    return fallback;
  }
}
function splitCsv(value) {
  return String(value || '').split(',').map(s => s.trim()).filter(Boolean);
}

function graphPayload(nodeIds=null) {
  const keep = nodeIds ? new Set(nodeIds) : null;
  return {
    nodes: state.nodes
      .filter(n => !keep || keep.has(n.id))
      .map(n => ({ id: n.id, type: n.type, params: n.params, position: n.position })),
    edges: state.edges
      .filter(e => !keep || (keep.has(e.from_node) && keep.has(e.to_node)))
      .map(e => ({ ...e })),
  };
}

function upstreamNodeIds(targetNodeId) {
  if (!targetNodeId) return [];
  const existing = new Set(state.nodes.map(n => n.id));
  if (!existing.has(targetNodeId)) return [];
  const incoming = new Map([...existing].map(id => [id, []]));
  for (const e of state.edges) {
    if (!existing.has(e.from_node) || !existing.has(e.to_node)) continue;
    incoming.get(e.to_node).push(e.from_node);
  }
  const seen = new Set([targetNodeId]);
  const stack = [targetNodeId];
  while (stack.length) {
    const cur = stack.pop();
    for (const parent of incoming.get(cur) || []) {
      if (!seen.has(parent)) {
        seen.add(parent);
        stack.push(parent);
      }
    }
  }
  return [...seen];
}


async function runGraph(scope='selected') {
  const results = el('results');
  results.className = 'results';

  let payload;
  let scopeText;
  if (scope === 'all') {
    payload = graphPayload();
    scopeText = `all nodes (${payload.nodes.length})`;
  } else {
    if (!state.selectedNodeId) {
      results.innerHTML = '<div class="result-card err"><div class="result-title">No selected node</div><pre>Please select a node first, then run to the selected node. Use Run All only when you really want to execute everything.</pre></div>';
      return;
    }
    const ids = upstreamNodeIds(state.selectedNodeId);
    payload = graphPayload(ids);
    scopeText = `upstream chain to ${state.selectedNodeId} (${payload.nodes.length} node${payload.nodes.length === 1 ? '' : 's'})`;
  }

  if (!payload.nodes.length) {
    results.innerHTML = '<div class="result-card err"><div class="result-title">Nothing to run</div><pre>The selected upstream chain is empty.</pre></div>';
    return;
  }

  results.innerHTML = `<div class="result-card"><b>Running ${escapeHtml(scopeText)}...</b></div>`;
  state.activeRunNodeIds = payload.nodes.map(n => n.id);
  state.runInProgress = true;
  setRunButtonsRunning(true);
  markNodesRunning(state.activeRunNodeIds);
  renderRunLog();
  startProgressPolling();
  try {
    const out = await api('/api/execute', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    state.lastExecution = out;
    if (out.results) {
      for (const [nodeId, result] of Object.entries(out.results)) state.nodeResults[nodeId] = result;
    }
    state.nodeStatuses = { ...state.nodeStatuses, ...(out.node_statuses || {}) };
    state.runLog = out.run_log || [];
    state.lastManifest = out.manifest || null;
    state.lastRunScope = {
      scope,
      scopeText,
      nodeIds: payload.nodes.map(n => n.id),
      executedOrder: out.executed_order || [],
      cachedOrder: out.cached_order || [],
    };
    renderResultsForSelected();
    renderInspector();
    renderNodes();
    renderRunLog();
    await refreshUploads();
    state.runInProgress = false;
    stopProgressPolling();
    setRunButtonsRunning(false);
  } catch (err) {
    state.lastExecution = null;
    state.lastRunScope = null;
    for (const n of payload.nodes) {
      if (state.nodeStatuses[n.id]?.status === 'running') state.nodeStatuses[n.id] = { status: 'idle' };
    }
    state.runInProgress = false;
    stopProgressPolling();
    setRunButtonsRunning(false);
    renderNodes();
    renderRunLog();
    results.innerHTML = `<div class="result-card err"><div class="result-title">Request failed</div><pre>${escapeHtml(String(err))}</pre></div>`;
  }
}

function renderResultsForSelected() {
  const root = el('results');
  if (!root) return;
  root.className = 'results';
  root.innerHTML = '';
  if (!state.selectedNodeId) {
    root.className = 'results empty';
    root.textContent = 'Select a node to see its execution result.';
    return;
  }
  if (state.lastExecution && !state.lastExecution.results && state.lastExecution.error) {
    root.appendChild(errorCard('Graph error', JSON.stringify(state.lastExecution, null, 2)));
    return;
  }
  const node = state.nodes.find(n => n.id === state.selectedNodeId);
  const res = state.nodeResults[state.selectedNodeId];
  if (!res) {
    root.className = 'results empty';
    root.textContent = 'Run to the selected node to see this node\'s result. Previously computed upstream nodes are reused automatically.';
    return;
  }
  root.appendChild(renderRunScopeNotice());
  if (node && node.type === 'Visualization' || node.type === 'Visualization') {
    const card = document.createElement('div');
    card.className = 'result-card ok compact-result-card';
    card.innerHTML = '<div class="result-title">Visualization <span>Shown inline in the node parameters panel</span></div><p class="muted">The connected data preview is displayed above in the right-side Node Parameters panel. Use the Enlarge/Open button there for an interactive PyVista window.</p>';
    root.appendChild(card);
  } else {
    root.appendChild(renderResultCard(state.selectedNodeId, res));
  }
}


function renderRunScopeNotice() {
  const div = document.createElement('div');
  div.className = 'run-scope';
  if (state.lastRunScope) {
    const ran = state.lastRunScope.executedOrder?.length ?? 0;
    const reused = state.lastRunScope.cachedOrder?.length ?? 0;
    div.textContent = `Last run: ${state.lastRunScope.scopeText}. Executed ${ran}, reused from cache ${reused}.`;
  } else {
    div.textContent = 'This result is from an earlier run.';
  }
  return div;
}

function errorCard(title, message) {
  const card = document.createElement('div');
  card.className = 'result-card err';
  card.innerHTML = `<div class="result-title">${escapeHtml(title)}</div><pre>${escapeHtml(message)}</pre>`;
  return card;
}

function renderResultCard(nodeId, res) {
  const card = document.createElement('div');
  card.className = `result-card ${res.ok ? 'ok' : 'err'}`;
  const cachedLabel = res.cached ? 'cached' : 'executed';
  const durationLabel = res.duration_ms !== undefined ? ` · ${res.duration_ms} ms` : '';
  card.innerHTML = `<div class="result-title">${escapeHtml(res.title || res.type || 'Node')} <span>${escapeHtml(nodeId)} · ${cachedLabel}${durationLabel}</span></div>`;
  if (!res.ok) {
    card.innerHTML += `<pre>${escapeHtml(res.error || 'Unknown error')}</pre>`;
    if (res.traceback) card.innerHTML += `<details><summary>Traceback</summary><pre>${escapeHtml(res.traceback)}</pre></details>`;
    return card;
  }
  for (const [portName, desc] of Object.entries(res.outputs || {})) {
    const block = document.createElement('div');
    block.className = 'result-port';
    block.innerHTML = `<h4>${escapeHtml(portName)} <span class="badge">${escapeHtml(desc.kind)}</span></h4>`;
    block.appendChild(renderPreview(desc.preview, { mode: 'result', showActions: false, compactJson: false }));
    card.appendChild(block);
  }
  return card;
}

function vtkReaderForFormat(format) {
  const vtkGlobal = window.vtk;
  if (!vtkGlobal) return null;
  const fmt = String(format || '').toLowerCase();

  const XML = vtkGlobal.IO?.XML || {};
  const make = (...factories) => {
    for (const f of factories) {
      try {
        if (f && f.newInstance) return f.newInstance();
      } catch (err) {
        console.warn('VTK reader factory failed', err);
      }
    }
    return null;
  };

  // Different vtk.js bundles expose XML readers under slightly different names.
  if (fmt === 'vtp') {
    return make(
      XML.vtkXMLPolyDataReader,
      XML.XMLPolyDataReader,
      vtkGlobal.IO?.XMLPolyDataReader,
      XML.vtkXMLReader,
      XML.XMLReader
    );
  }
  if (fmt === 'vtu') {
    return make(
      XML.vtkXMLUnstructuredGridReader,
      XML.XMLUnstructuredGridReader,
      vtkGlobal.IO?.XMLUnstructuredGridReader,
      XML.vtkXMLReader,
      XML.XMLReader
    );
  }
  if (fmt === 'vti') {
    return make(
      XML.vtkXMLImageDataReader,
      XML.XMLImageDataReader,
      vtkGlobal.IO?.XMLImageDataReader,
      XML.vtkXMLReader,
      XML.XMLReader
    );
  }

  // Legacy .vtk is not consistently supported in vtk.js. The backend should
  // create a VTP surface preview for inline display.
  return null;
}


function inferVtkFormatFromPreview(preview) {
  const explicit = String(preview.web_mesh_format || '').toLowerCase().replace(/^\./, '');
  if (explicit) return explicit;
  const name = String(preview.spatial_mesh_file_name || preview.file_name || preview.name || '').toLowerCase();
  const m = name.match(/\.([a-z0-9]+)$/);
  return m ? m[1] : 'vtp';
}

function createWebViewerPlaceholder(message) {
  const div = document.createElement('div');
  div.className = 'web-vtk-message';
  div.textContent = message;
  return div;
}

function resizeVTKViewer(genericRenderWindow) {
  try {
    genericRenderWindow.resize();
    genericRenderWindow.getRenderWindow().render();
  } catch (err) {
    console.warn('VTK resize failed', err);
  }
}

function vtkDataArrays(dataset, association) {
  try {
    const attr = association === 'cell' ? dataset.getCellData() : dataset.getPointData();
    if (!attr || !attr.getArrays) return [];
    return attr.getArrays()
      .filter(a => a && a.getName && a.getName())
      .map(a => ({
        name: a.getName(),
        association,
        components: a.getNumberOfComponents ? a.getNumberOfComponents() : 1,
        data: a.getData ? a.getData() : null,
      }))
      .filter(a => a.components === 1 && a.data && a.data.length);
  } catch (err) {
    console.warn('Could not list VTK arrays', err);
    return [];
  }
}

function allScalarArrays(dataset) {
  return [...vtkDataArrays(dataset, 'point'), ...vtkDataArrays(dataset, 'cell')];
}

function arrayRange(data) {
  let min = Infinity;
  let max = -Infinity;
  for (let i = 0; i < data.length; i++) {
    const v = Number(data[i]);
    if (!Number.isFinite(v)) continue;
    if (v < min) min = v;
    if (v > max) max = v;
  }
  return Number.isFinite(min) ? [min, max] : [0, 1];
}

function uniqueScalarValues(data, maxCount=64) {
  const out = [];
  for (let i = 0; i < data.length; i++) {
    const v = Number(data[i]);
    if (!Number.isFinite(v)) continue;
    if (!out.includes(v)) out.push(v);
    if (out.length >= maxCount) break;
  }
  return out.sort((a, b) => a - b);
}

const WEB_VTK_PALETTE = [
  [0.478, 0.122, 0.635], // purple
  [0.820, 0.478, 0.776], // pink
  [0.122, 0.439, 0.800], // blue
  [0.933, 0.792, 0.231], // yellow
  [0.173, 0.627, 0.173], // green
  [0.839, 0.153, 0.157], // red
  [1.000, 0.498, 0.055], // orange
  [0.090, 0.745, 0.812], // cyan
  [0.580, 0.404, 0.741],
  [0.549, 0.337, 0.294],
];

function rgbToCss(rgb) {
  return `rgb(${Math.round(rgb[0] * 255)}, ${Math.round(rgb[1] * 255)}, ${Math.round(rgb[2] * 255)})`;
}

function createCategoricalLookupTable(values) {
  const vtkGlobal = window.vtk;
  const ctf = vtkGlobal.Rendering.Core.vtkColorTransferFunction.newInstance();
  const vals = values.length ? values : [0, 1];

  vals.forEach((v, idx) => {
    const color = WEB_VTK_PALETTE[idx % WEB_VTK_PALETTE.length];
    // Duplicate points very close to each value to make categorical colors
    // sharper when the color transfer function interpolates.
    ctf.addRGBPoint(v - 1e-6, color[0], color[1], color[2]);
    ctf.addRGBPoint(v, color[0], color[1], color[2]);
    ctf.addRGBPoint(v + 1e-6, color[0], color[1], color[2]);
  });
  return ctf;
}

function createContinuousLookupTable(range) {
  const vtkGlobal = window.vtk;
  const ctf = vtkGlobal.Rendering.Core.vtkColorTransferFunction.newInstance();
  const [min, max] = range;
  const mid = (min + max) / 2;
  ctf.addRGBPoint(min, 0.231, 0.298, 0.753);
  ctf.addRGBPoint(mid, 0.865, 0.865, 0.865);
  ctf.addRGBPoint(max, 0.706, 0.016, 0.150);
  return ctf;
}

function labelForScalarValue(preview, value) {
  if (preview.formation_id_map && typeof preview.formation_id_map === 'object') {
    for (const [name, id] of Object.entries(preview.formation_id_map)) {
      if (Number(id) === Number(value)) return name;
    }
  }
  return String(value);
}

function applyScalarColoring(mapper, dataset, arrayInfo, preview) {
  if (!arrayInfo || !arrayInfo.name) {
    try {
      mapper.setScalarVisibility(false);
    } catch {}
    return null;
  }

  const vtkGlobal = window.vtk;
  const data = arrayInfo.data;
  const range = arrayRange(data);
  const uniqueValues = uniqueScalarValues(data, 32);
  const isCategorical =
    uniqueValues.length > 0 &&
    uniqueValues.length <= 16 &&
    uniqueValues.every(v => Math.abs(v - Math.round(v)) < 1e-8);

  try {
    mapper.setScalarVisibility(true);
    if (mapper.setColorModeToMapScalars) mapper.setColorModeToMapScalars();
    if (mapper.setUseLookupTableScalarRange) mapper.setUseLookupTableScalarRange(true);

    const vtkMapper = vtkGlobal.Rendering.Core.vtkMapper;
    if (vtkMapper?.ScalarMode) {
      if (arrayInfo.association === 'point' && vtkMapper.ScalarMode.USE_POINT_FIELD_DATA !== undefined) {
        mapper.setScalarMode(vtkMapper.ScalarMode.USE_POINT_FIELD_DATA);
      } else if (arrayInfo.association === 'cell' && vtkMapper.ScalarMode.USE_CELL_FIELD_DATA !== undefined) {
        mapper.setScalarMode(vtkMapper.ScalarMode.USE_CELL_FIELD_DATA);
      }
    } else {
      if (arrayInfo.association === 'point' && mapper.setScalarModeToUsePointFieldData) mapper.setScalarModeToUsePointFieldData();
      if (arrayInfo.association === 'cell' && mapper.setScalarModeToUseCellFieldData) mapper.setScalarModeToUseCellFieldData();
    }

    if (vtkMapper?.GetArray?.BY_NAME !== undefined && mapper.setArrayAccessMode) {
      mapper.setArrayAccessMode(vtkMapper.GetArray.BY_NAME);
    }

    if (mapper.setColorByArrayName) mapper.setColorByArrayName(arrayInfo.name);
    if (mapper.setScalarRange) mapper.setScalarRange(range[0], range[1]);

    const lut = isCategorical ? createCategoricalLookupTable(uniqueValues) : createContinuousLookupTable(range);
    if (mapper.setLookupTable) mapper.setLookupTable(lut);

    return { range, uniqueValues, isCategorical, lut };
  } catch (err) {
    console.warn('Scalar coloring failed:', err);
    try {
      mapper.setScalarVisibility(false);
    } catch {}
    return null;
  }
}

function makeScalarSelector(viewerState, preview) {
  const arrays = viewerState.scalarArrays;
  const toolbar = document.createElement('div');
  toolbar.className = 'web-vtk-controls';

  const scalarLabel = document.createElement('label');
  scalarLabel.textContent = 'Scalar';
  const select = document.createElement('select');
  select.className = 'web-vtk-select';

  const none = document.createElement('option');
  none.value = '';
  none.textContent = 'Solid color';
  select.appendChild(none);

  arrays.forEach((a, idx) => {
    const opt = document.createElement('option');
    opt.value = String(idx);
    opt.textContent = `${a.name} (${a.association})`;
    if (a.name === preview.web_scalar || (!preview.web_scalar && idx === 0)) opt.selected = true;
    select.appendChild(opt);
  });

  scalarLabel.appendChild(select);
  toolbar.appendChild(scalarLabel);

  const edgeLabel = document.createElement('label');
  edgeLabel.className = 'inline-check web-vtk-checkbox';
  const edgeCb = document.createElement('input');
  edgeCb.type = 'checkbox';
  edgeCb.checked = !!preview.web_show_edges;
  edgeLabel.appendChild(edgeCb);
  edgeLabel.appendChild(document.createTextNode(' edges'));
  toolbar.appendChild(edgeLabel);

  const axesLabel = document.createElement('label');
  axesLabel.className = 'inline-check web-vtk-checkbox';
  const axesCb = document.createElement('input');
  axesCb.type = 'checkbox';
  axesCb.checked = true;
  axesLabel.appendChild(axesCb);
  axesLabel.appendChild(document.createTextNode(' axes'));
  toolbar.appendChild(axesLabel);

  const opacityLabel = document.createElement('label');
  opacityLabel.textContent = 'Opacity';
  const opacity = document.createElement('input');
  opacity.type = 'range';
  opacity.min = '0.05';
  opacity.max = '1';
  opacity.step = '0.05';
  opacity.value = String(preview.web_opacity || 1);
  opacityLabel.appendChild(opacity);
  toolbar.appendChild(opacityLabel);

  const update = () => {
    const idx = select.value === '' ? -1 : Number(select.value);
    const arr = idx >= 0 ? arrays[idx] : null;
    const coloring = applyScalarColoring(viewerState.mapper, viewerState.dataset, arr, preview);
    try {
      viewerState.actor.getProperty().setEdgeVisibility(edgeCb.checked);
      viewerState.actor.getProperty().setOpacity(Number(opacity.value));
    } catch {}

    if (viewerState.cubeAxesActor) {
      try {
        viewerState.cubeAxesActor.setVisibility(axesCb.checked);
      } catch {}
    }
    renderScalarLegend(viewerState.legendContainer, preview, arr, coloring);
    viewerState.renderWindow.render();
  };

  select.onchange = update;
  edgeCb.onchange = update;
  axesCb.onchange = update;
  opacity.oninput = update;

  setTimeout(update, 0);
  return toolbar;
}

function renderScalarLegend(container, preview, arrayInfo, coloring) {
  if (!container) return;
  container.innerHTML = '';

  if (!arrayInfo || !coloring) {
    container.textContent = 'Legend: solid color';
    return;
  }

  const title = document.createElement('div');
  title.className = 'web-vtk-legend-title';
  title.textContent = arrayInfo.name;
  container.appendChild(title);

  if (coloring.isCategorical) {
    const vals = coloring.uniqueValues.slice(0, 16);
    vals.forEach((v, idx) => {
      const row = document.createElement('div');
      row.className = 'web-vtk-legend-row';
      const sw = document.createElement('span');
      sw.className = 'web-vtk-swatch';
      sw.style.background = rgbToCss(WEB_VTK_PALETTE[idx % WEB_VTK_PALETTE.length]);
      const label = document.createElement('span');
      label.textContent = labelForScalarValue(preview, v);
      row.appendChild(sw);
      row.appendChild(label);
      container.appendChild(row);
    });
  } else {
    const [min, max] = coloring.range;
    const grad = document.createElement('div');
    grad.className = 'web-vtk-gradient';
    container.appendChild(grad);
    const nums = document.createElement('div');
    nums.className = 'web-vtk-gradient-labels';
    nums.innerHTML = `<span>${min.toPrecision(4)}</span><span>${max.toPrecision(4)}</span>`;
    container.appendChild(nums);
  }
}

function addAxesActors(vtkGlobal, renderer, dataset) {
  let cubeAxesActor = null;
  let orientationWidget = null;

  try {
    if (vtkGlobal.Rendering.Core.vtkCubeAxesActor) {
      cubeAxesActor = vtkGlobal.Rendering.Core.vtkCubeAxesActor.newInstance();
      cubeAxesActor.setCamera(renderer.getActiveCamera());
      cubeAxesActor.setDataBounds(dataset.getBounds());
      renderer.addActor(cubeAxesActor);
    }
  } catch (err) {
    console.warn('Cube axes actor unavailable:', err);
  }

  try {
    if (vtkGlobal.Rendering.Core.vtkAxesActor && vtkGlobal.Interaction?.Widgets?.vtkOrientationMarkerWidget) {
      const axes = vtkGlobal.Rendering.Core.vtkAxesActor.newInstance();
      orientationWidget = vtkGlobal.Interaction.Widgets.vtkOrientationMarkerWidget.newInstance({
        actor: axes,
        interactor: renderer.getRenderWindow().getInteractor(),
      });
      orientationWidget.setEnabled(true);
      if (orientationWidget.setViewportCorner && vtkGlobal.Interaction.Widgets.vtkOrientationMarkerWidget.Corners) {
        orientationWidget.setViewportCorner(vtkGlobal.Interaction.Widgets.vtkOrientationMarkerWidget.Corners.BOTTOM_RIGHT);
      }
      if (orientationWidget.setViewportSize) orientationWidget.setViewportSize(0.18);
      if (orientationWidget.setMinPixelSize) orientationWidget.setMinPixelSize(80);
      if (orientationWidget.setMaxPixelSize) orientationWidget.setMaxPixelSize(180);
    }
  } catch (err) {
    console.warn('Orientation axes widget unavailable:', err);
  }

  return { cubeAxesActor, orientationWidget };
}


function nearestEditorPointFromPosition(preview, pos) {
  const points = preview.editor_points || [];
  if (!points.length || !pos) return null;
  let best = null;
  let bestD2 = Infinity;
  for (const p of points) {
    const dx = Number(p.x) - Number(pos.x);
    const dy = Number(p.y) - Number(pos.y);
    const dz = Number(p.z) - Number(pos.z);
    const d2 = dx * dx + dy * dy + dz * dz;
    if (d2 < bestD2) {
      bestD2 = d2;
      best = p;
    }
  }
  return best;
}

function installEditorPicking(container, preview, viewerState) {
  if (!preview || !preview.editor_picker_enabled || !Array.isArray(preview.editor_points)) return;
  const vtkGlobal = window.vtk;
  const interactor = viewerState.renderWindow?.getInteractor?.();
  const renderer = viewerState.renderer;
  if (!interactor || !renderer) return;

  const pickerFactory =
    vtkGlobal.Rendering?.Core?.vtkPointPicker ||
    vtkGlobal.Rendering?.Core?.vtkCellPicker ||
    vtkGlobal.Rendering?.Core?.vtkPicker;
  const picker = pickerFactory?.newInstance ? pickerFactory.newInstance() : null;
  if (picker?.setTolerance) picker.setTolerance(0.03);

  const firePicked = (point, position) => {
    if (!point && position) point = nearestEditorPointFromPosition(preview, position);
    if (!point) return;
    container.dispatchEvent(new CustomEvent('vtk-point-picked', {
      detail: { point, position: position || { x: point.x, y: point.y, z: point.z } },
      bubbles: true,
    }));
  };

  try {
    interactor.onLeftButtonPress((callData) => {
      const p = callData?.position || callData?.event?.position || null;
      if (!p || !picker) return;
      try {
        picker.pick([p.x, p.y, 0], renderer);
        let posArr = picker.getPickPosition ? picker.getPickPosition() : null;
        const position = posArr ? { x: Number(posArr[0]), y: Number(posArr[1]), z: Number(posArr[2]) } : null;
        let id = -1;
        if (picker.getPointId) id = picker.getPointId();
        else if (picker.getCellId) id = picker.getCellId();

        let point = null;
        if (Number.isInteger(id) && id >= 0 && preview.editor_points[id]) {
          point = preview.editor_points[id];
        }
        firePicked(point, position);
      } catch (err) {
        console.warn('Interactive point picking failed:', err);
      }
    });
  } catch (err) {
    console.warn('Could not install editor picking:', err);
  }
}



function renderHtmlPointCloudFallback(preview) {
  const wrap = document.createElement('div');
  wrap.className = 'html-point-fallback';

  const toolbar = document.createElement('div');
  toolbar.className = 'web-vtk-controls';
  const projLabel = document.createElement('label');
  projLabel.textContent = 'Projection ';
  const projection = document.createElement('select');
  ['XY', 'XZ', 'YZ'].forEach(v => {
    const opt = document.createElement('option');
    opt.value = v;
    opt.textContent = v;
    projection.appendChild(opt);
  });
  projLabel.appendChild(projection);
  toolbar.appendChild(projLabel);

  const info = document.createElement('span');
  info.className = 'web-vtk-info';
  info.textContent = 'HTML fallback: VTK.js not loaded. Click points to select them.';
  toolbar.appendChild(info);
  wrap.appendChild(toolbar);

  const canvas = document.createElement('canvas');
  canvas.className = 'html-point-canvas';
  canvas.width = 640;
  canvas.height = 420;
  wrap.appendChild(canvas);

  const caption = document.createElement('div');
  caption.className = 'param-help';
  caption.textContent = 'This fallback works without internet/VTK.js. It supports visual point selection in projected views.';
  wrap.appendChild(caption);

  const points = preview.editor_points || [];
  let projected = [];

  function axesForProjection() {
    const p = projection.value || 'XY';
    if (p === 'XZ') return ['x', 'z', 'y'];
    if (p === 'YZ') return ['y', 'z', 'x'];
    return ['x', 'y', 'z'];
  }

  function draw() {
    const ctx = canvas.getContext('2d');
    const w = canvas.width;
    const h = canvas.height;
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = '#070b12';
    ctx.fillRect(0, 0, w, h);

    const [ax, ay] = axesForProjection();
    const valsX = points.map(p => Number(p[ax])).filter(Number.isFinite);
    const valsY = points.map(p => Number(p[ay])).filter(Number.isFinite);
    if (!valsX.length || !valsY.length) {
      ctx.fillStyle = '#cbd5e1';
      ctx.fillText('No editable points available.', 16, 24);
      return;
    }

    let xmin = Math.min(...valsX), xmax = Math.max(...valsX);
    let ymin = Math.min(...valsY), ymax = Math.max(...valsY);
    if (xmin === xmax) { xmin -= 1; xmax += 1; }
    if (ymin === ymax) { ymin -= 1; ymax += 1; }
    const pad = 32;
    const sx = (w - 2 * pad) / (xmax - xmin);
    const sy = (h - 2 * pad) / (ymax - ymin);

    ctx.strokeStyle = 'rgba(148, 163, 184, 0.35)';
    ctx.lineWidth = 1;
    ctx.strokeRect(pad, pad, w - 2 * pad, h - 2 * pad);
    ctx.fillStyle = '#94a3b8';
    ctx.font = '12px sans-serif';
    ctx.fillText(`${ax.toUpperCase()} →`, w - 55, h - 12);
    ctx.fillText(`${ay.toUpperCase()} ↑`, 10, 22);

    projected = points.map((p, idx) => {
      const x = pad + (Number(p[ax]) - xmin) * sx;
      const y = h - pad - (Number(p[ay]) - ymin) * sy;
      return { idx, x, y, point: p };
    });

    for (const q of projected) {
      const p = q.point;
      const isOri = p.kind === 'orientation';
      const fid = preview.formation_id_map && p.element in preview.formation_id_map ? Number(preview.formation_id_map[p.element]) : q.idx;
      const color = WEB_VTK_PALETTE[Math.abs(fid) % WEB_VTK_PALETTE.length];
      ctx.fillStyle = rgbToCss(color);
      ctx.strokeStyle = isOri ? '#ffffff' : 'rgba(255,255,255,0.35)';
      ctx.lineWidth = isOri ? 2 : 1;
      ctx.beginPath();
      ctx.arc(q.x, q.y, isOri ? 6 : 5, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();

      if (isOri && Number.isFinite(Number(p.gx)) && Number.isFinite(Number(p.gy)) && Number.isFinite(Number(p.gz))) {
        const scale = 18;
        const gx = ax === 'x' ? p.gx : ax === 'y' ? p.gy : p.gz;
        const gy = ay === 'x' ? p.gx : ay === 'y' ? p.gy : p.gz;
        ctx.strokeStyle = '#ffffff';
        ctx.beginPath();
        ctx.moveTo(q.x, q.y);
        ctx.lineTo(q.x + Number(gx) * scale, q.y - Number(gy) * scale);
        ctx.stroke();
      }
    }
  }

  canvas.addEventListener('click', (ev) => {
    const rect = canvas.getBoundingClientRect();
    const x = (ev.clientX - rect.left) * (canvas.width / rect.width);
    const y = (ev.clientY - rect.top) * (canvas.height / rect.height);
    let best = null;
    let bestD2 = Infinity;
    for (const q of projected) {
      const dx = q.x - x;
      const dy = q.y - y;
      const d2 = dx * dx + dy * dy;
      if (d2 < bestD2) {
        bestD2 = d2;
        best = q;
      }
    }
    if (best && bestD2 < 20 * 20) {
      wrap.dispatchEvent(new CustomEvent('vtk-point-picked', {
        detail: {
          point: best.point,
          position: { x: best.point.x, y: best.point.y, z: best.point.z },
          fallback: true,
        },
        bubbles: true,
      }));
    }
  });

  projection.onchange = draw;
  setTimeout(draw, 0);
  return wrap;
}



function numericRange(values) {
  let min = Infinity;
  let max = -Infinity;
  for (const v of values || []) {
    const n = Number(v);
    if (!Number.isFinite(n)) continue;
    if (n < min) min = n;
    if (n > max) max = n;
  }
  return Number.isFinite(min) ? [min, max] : [0, 1];
}

function colorForValue(value, values) {
  const unique = uniqueScalarValues(values || [], 32);
  const categorical = unique.length > 0 && unique.length <= 24 && unique.every(v => Math.abs(v - Math.round(v)) < 1e-8);
  if (categorical) {
    const idx = Math.max(unique.findIndex(v => Number(v) === Number(value)), 0);
    return rgbToCss(WEB_VTK_PALETTE[idx % WEB_VTK_PALETTE.length]);
  }
  const [min, max] = numericRange(values || []);
  const t = max > min ? Math.max(0, Math.min(1, (Number(value) - min) / (max - min))) : 0.5;
  const r = Math.round(60 + 180 * t);
  const g = Math.round(120 + 80 * (1 - Math.abs(t - 0.5) * 2));
  const b = Math.round(220 - 170 * t);
  return `rgb(${r},${g},${b})`;
}

function renderSimpleMeshFallback(preview) {
  const wrap = document.createElement('div');
  wrap.className = 'simple-mesh-fallback';

  const toolbar = document.createElement('div');
  toolbar.className = 'web-vtk-controls';

  const title = document.createElement('span');
  title.className = 'web-vtk-info';
  title.textContent = 'Built-in viewer: loading mesh preview...';
  toolbar.appendChild(title);

  const scalarLabel = document.createElement('label');
  scalarLabel.textContent = 'Scalar ';
  const scalarSelect = document.createElement('select');
  scalarLabel.appendChild(scalarSelect);
  toolbar.appendChild(scalarLabel);

  const edgeLabel = document.createElement('label');
  edgeLabel.className = 'inline-check web-vtk-checkbox';
  const edgeCb = document.createElement('input');
  edgeCb.type = 'checkbox';
  edgeCb.checked = !!preview.web_show_edges;
  edgeLabel.appendChild(edgeCb);
  edgeLabel.appendChild(document.createTextNode(' edges'));
  toolbar.appendChild(edgeLabel);

  const resetBtn = document.createElement('button');
  resetBtn.type = 'button';
  resetBtn.className = 'mini';
  resetBtn.textContent = 'Reset view';
  toolbar.appendChild(resetBtn);

  wrap.appendChild(toolbar);

  const canvas = document.createElement('canvas');
  canvas.className = 'simple-mesh-canvas';
  canvas.width = 760;
  canvas.height = 460;
  wrap.appendChild(canvas);

  const help = document.createElement('div');
  help.className = 'param-help';
  help.textContent = 'VTK.js is unavailable, so this local fallback viewer is used. Drag to rotate, wheel to zoom. Use the PyVista popup/download for full VTK inspection.';
  wrap.appendChild(help);

  const stateLocal = {
    mesh: null,
    yaw: -0.65,
    pitch: 0.45,
    zoom: 1.0,
    dragging: false,
    lastX: 0,
    lastY: 0,
  };

  function scalarArrays(mesh) {
    const out = [];
    for (const [name, data] of Object.entries(mesh.cellScalars || {})) out.push({ name, association: 'cell', data });
    for (const [name, data] of Object.entries(mesh.pointScalars || {})) out.push({ name, association: 'point', data });
    return out;
  }

  function fillScalarSelect(mesh) {
    scalarSelect.innerHTML = '';
    const none = document.createElement('option');
    none.value = '';
    none.textContent = 'Solid';
    scalarSelect.appendChild(none);
    const arrays = scalarArrays(mesh);
    arrays.forEach((arr, idx) => {
      const opt = document.createElement('option');
      opt.value = String(idx);
      opt.textContent = `${arr.name} (${arr.association})`;
      if (arr.name === preview.web_scalar || (!preview.web_scalar && idx === 0)) opt.selected = true;
      scalarSelect.appendChild(opt);
    });
  }

  function selectedScalar(mesh) {
    const idx = Number(scalarSelect.value);
    const arrays = scalarArrays(mesh);
    if (Number.isInteger(idx) && arrays[idx]) return arrays[idx];
    return null;
  }

  function projectPoints(mesh) {
    const pts = mesh.points || [];
    if (!pts.length) return [];
    let cx = 0, cy = 0, cz = 0;
    for (const p of pts) { cx += p[0]; cy += p[1]; cz += p[2]; }
    cx /= pts.length; cy /= pts.length; cz /= pts.length;

    let scale = 1;
    const b = Array.isArray(mesh.bounds) && mesh.bounds.length === 6 ? mesh.bounds : null;
    if (b) {
      const dx = Number(b[1]) - Number(b[0]);
      const dy = Number(b[3]) - Number(b[2]);
      const dz = Number(b[5]) - Number(b[4]);
      scale = Math.max(dx, dy, dz, 1);
    } else {
      let maxR = 1;
      for (const p of pts) {
        const r = Math.hypot(p[0] - cx, p[1] - cy, p[2] - cz);
        if (r > maxR) maxR = r;
      }
      scale = maxR * 2;
    }

    const cyaw = Math.cos(stateLocal.yaw), syaw = Math.sin(stateLocal.yaw);
    const cp = Math.cos(stateLocal.pitch), sp = Math.sin(stateLocal.pitch);
    const w = canvas.width, h = canvas.height;
    const s = Math.min(w, h) * 0.72 * stateLocal.zoom / scale;

    return pts.map(p => {
      let x = Number(p[0]) - cx;
      let y = Number(p[1]) - cy;
      let z = Number(p[2]) - cz;
      const x1 = cyaw * x + syaw * z;
      const z1 = -syaw * x + cyaw * z;
      const y1 = cp * y - sp * z1;
      const z2 = sp * y + cp * z1;
      return {
        x: w / 2 + x1 * s,
        y: h / 2 - y1 * s,
        z: z2,
      };
    });
  }

  function drawAxes(ctx) {
    ctx.save();
    ctx.font = '12px sans-serif';
    ctx.lineWidth = 2;
    const x0 = 35, y0 = canvas.height - 35;
    const axes = [
      ['X', 1, 0],
      ['Y', 0, -1],
      ['Z', -0.55, -0.55],
    ];
    for (const [label, dx, dy] of axes) {
      ctx.strokeStyle = '#94a3b8';
      ctx.fillStyle = '#cbd5e1';
      ctx.beginPath();
      ctx.moveTo(x0, y0);
      ctx.lineTo(x0 + dx * 34, y0 + dy * 34);
      ctx.stroke();
      ctx.fillText(label, x0 + dx * 40, y0 + dy * 40);
    }
    ctx.restore();
  }

  function draw() {
    const mesh = stateLocal.mesh;
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = '#050914';
    ctx.fillRect(0, 0, canvas.width, canvas.height);

    if (!mesh || !mesh.points || !mesh.points.length) {
      ctx.fillStyle = '#cbd5e1';
      ctx.fillText('No fallback mesh data available.', 20, 30);
      return;
    }

    const proj = projectPoints(mesh);
    const faces = mesh.faces || [];
    const scalar = selectedScalar(mesh);

    const drawable = faces.map((face, idx) => {
      const pts = face.map(i => proj[Number(i)]).filter(Boolean);
      const z = pts.length ? pts.reduce((a, p) => a + p.z, 0) / pts.length : 0;
      let value = idx;
      if (scalar) {
        if (scalar.association === 'cell') value = scalar.data[idx] ?? 0;
        else if (scalar.association === 'point' && face.length) {
          let sum = 0, count = 0;
          for (const i of face) {
            const v = scalar.data[Number(i)];
            if (Number.isFinite(Number(v))) { sum += Number(v); count++; }
          }
          value = count ? sum / count : 0;
        }
      }
      return { face, pts, z, idx, value };
    }).sort((a, b) => a.z - b.z);

    const drawAsPoints = !faces.length || faces.every(f => f.length <= 1);

    if (drawAsPoints) {
      const pointScalar = scalar && scalar.association === 'point' ? scalar : null;
      for (let i = 0; i < proj.length; i++) {
        const p = proj[i];
        const value = pointScalar ? pointScalar.data[i] : i;
        ctx.fillStyle = pointScalar ? colorForValue(value, pointScalar.data) : '#38bdf8';
        ctx.beginPath();
        ctx.arc(p.x, p.y, 3.5, 0, Math.PI * 2);
        ctx.fill();
      }
    } else {
      for (const item of drawable) {
        if (!item.pts.length) continue;
        ctx.beginPath();
        ctx.moveTo(item.pts[0].x, item.pts[0].y);
        for (let i = 1; i < item.pts.length; i++) ctx.lineTo(item.pts[i].x, item.pts[i].y);
        if (item.pts.length > 2) ctx.closePath();

        ctx.fillStyle = scalar ? colorForValue(item.value, scalar.data) : 'rgba(56, 189, 248, 0.72)';
        ctx.strokeStyle = 'rgba(15, 23, 42, 0.35)';
        ctx.lineWidth = 0.5;
        if (item.pts.length > 2) ctx.fill();
        if (edgeCb.checked || item.pts.length <= 2) ctx.stroke();
      }
    }

    drawAxes(ctx);

    ctx.fillStyle = '#cbd5e1';
    ctx.font = '12px sans-serif';
    const extra = mesh.truncated ? ` | truncated from ${mesh.original_n_points || '?'} pts / ${mesh.original_n_cells || '?'} cells` : '';
    ctx.fillText(`${mesh.name || 'mesh'} | points=${mesh.n_points || mesh.points.length} | faces=${mesh.n_cells || faces.length}${extra}`, 12, 18);
  }

  canvas.addEventListener('mousedown', (ev) => {
    stateLocal.dragging = true;
    stateLocal.lastX = ev.clientX;
    stateLocal.lastY = ev.clientY;
  });
  window.addEventListener('mouseup', () => { stateLocal.dragging = false; });
  window.addEventListener('mousemove', (ev) => {
    if (!stateLocal.dragging) return;
    const dx = ev.clientX - stateLocal.lastX;
    const dy = ev.clientY - stateLocal.lastY;
    stateLocal.lastX = ev.clientX;
    stateLocal.lastY = ev.clientY;
    stateLocal.yaw += dx * 0.01;
    stateLocal.pitch += dy * 0.01;
    stateLocal.pitch = Math.max(-1.45, Math.min(1.45, stateLocal.pitch));
    draw();
  });
  canvas.addEventListener('wheel', (ev) => {
    ev.preventDefault();
    stateLocal.zoom *= ev.deltaY < 0 ? 1.12 : 0.89;
    stateLocal.zoom = Math.max(0.08, Math.min(30, stateLocal.zoom));
    draw();
  }, { passive: false });

  resetBtn.onclick = () => {
    stateLocal.yaw = -0.65;
    stateLocal.pitch = 0.45;
    stateLocal.zoom = 1.0;
    draw();
  };
  scalarSelect.onchange = draw;
  edgeCb.onchange = draw;

  (async () => {
    try {
      const res = await fetch(preview.web_mesh_json_url);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const mesh = await res.json();
      stateLocal.mesh = mesh;
      fillScalarSelect(mesh);
      title.textContent = 'Built-in mesh viewer';
      draw();
    } catch (err) {
      title.textContent = `Built-in viewer failed: ${err.message || err}`;
      draw();
    }
  })();

  return wrap;
}


async function initWebVTKViewer(container, preview, options={}) {
  if (!container || container.dataset.initialized === 'true') return;
  container.dataset.initialized = 'true';

  if (!window.vtk) {
    if (preview.editor_picker_enabled && Array.isArray(preview.editor_points)) {
      container.appendChild(renderHtmlPointCloudFallback(preview));
      return;
    }
    if (preview.web_mesh_json_url) {
      container.appendChild(renderSimpleMeshFallback(preview));
      return;
    }
    if (preview.image_url) {
      const img = document.createElement('img');
      img.className = 'inline-plot-preview';
      img.src = preview.image_url;
      img.alt = preview.file_name || preview.name || 'preview image';
      container.appendChild(img);
      return;
    }
    container.appendChild(createWebViewerPlaceholder('VTK.js is not loaded and no local fallback mesh was generated. Re-run the node with the v69 backend, or use the downloadable mesh/PyVista popup.'));
    return;
  }

  const vtkGlobal = window.vtk;
  const meshUrl = preview.web_mesh_url;
  if (!meshUrl) {
    container.appendChild(createWebViewerPlaceholder('No web mesh URL was provided for this preview.'));
    return;
  }

  const format = inferVtkFormatFromPreview(preview);
  const reader = vtkReaderForFormat(format);
  if (!reader) {
    container.appendChild(createWebViewerPlaceholder(`Inline viewer could not find a browser reader for this file. The app should generate a VTP surface preview; current format is "${format}". Download the file instead.`));
    return;
  }

  container.innerHTML = '';
  const controlsHost = document.createElement('div');
  controlsHost.className = 'web-vtk-controls-host';

  const layout = document.createElement('div');
  layout.className = 'web-vtk-layout';

  const canvasHost = document.createElement('div');
  canvasHost.className = 'web-vtk-canvas-host';

  const legend = document.createElement('div');
  legend.className = 'web-vtk-legend';

  layout.appendChild(canvasHost);
  layout.appendChild(legend);
  container.appendChild(controlsHost);
  container.appendChild(layout);

  try {
    const response = await fetch(meshUrl);
    if (!response.ok) throw new Error(`Could not fetch mesh: HTTP ${response.status}`);
    const buffer = await response.arrayBuffer();

    reader.parseAsArrayBuffer(buffer);
    const dataset = reader.getOutputData(0);

    const genericRenderWindow = vtkGlobal.Rendering.Misc.vtkGenericRenderWindow.newInstance({
      background: [0.03, 0.04, 0.06],
    });
    genericRenderWindow.setContainer(canvasHost);

    const renderer = genericRenderWindow.getRenderer();
    const renderWindow = genericRenderWindow.getRenderWindow();

    const mapper = vtkGlobal.Rendering.Core.vtkMapper.newInstance();
    mapper.setInputData(dataset);

    const actor = vtkGlobal.Rendering.Core.vtkActor.newInstance();
    actor.setMapper(mapper);

    try {
      actor.getProperty().setEdgeVisibility(!!preview.web_show_edges || !!options.showEdges);
      actor.getProperty().setOpacity(Number(preview.web_opacity || options.opacity || 1));
      actor.getProperty().setPointSize(9);
      if (actor.getProperty().setRenderPointsAsSpheres) actor.getProperty().setRenderPointsAsSpheres(true);
    } catch (err) {
      console.warn('Actor property setup failed:', err);
    }

    renderer.addActor(actor);
    const axesState = addAxesActors(vtkGlobal, renderer, dataset);

    const scalarArrays = allScalarArrays(dataset);
    const viewerState = {
      genericRenderWindow,
      renderer,
      renderWindow,
      actor,
      mapper,
      dataset,
      scalarArrays,
      legendContainer: legend,
      cubeAxesActor: axesState.cubeAxesActor,
      orientationWidget: axesState.orientationWidget,
    };

    controlsHost.appendChild(makeScalarSelector(viewerState, preview));

    renderer.resetCamera();
    renderWindow.render();

    container._vtkViewer = viewerState;
    installEditorPicking(container, preview, viewerState);

    setTimeout(() => resizeVTKViewer(genericRenderWindow), 50);
    window.addEventListener('resize', () => resizeVTKViewer(genericRenderWindow), { passive: true });

    const info = document.createElement('div');
    info.className = 'web-vtk-info';
    const cells = preview.n_cells ?? (dataset.getNumberOfCells ? dataset.getNumberOfCells() : '');
    const points = preview.n_points ?? (dataset.getNumberOfPoints ? dataset.getNumberOfPoints() : '');
    info.textContent = `Web viewer: ${format.toUpperCase()} | cells=${cells} | points=${points}`;
    container.appendChild(info);
  } catch (err) {
    console.error(err);
    container.innerHTML = '';
    container.appendChild(createWebViewerPlaceholder(`Web 3D viewer failed: ${err.message || err}`));
  }
}

function openWebViewerModal(preview) {
  const overlay = document.createElement('div');
  overlay.className = 'web-vtk-modal-overlay';
  overlay.innerHTML = `
    <div class="web-vtk-modal">
      <div class="web-vtk-modal-header">
        <b>${escapeHtml(preview.name || preview.file_name || 'Web 3D Viewer')}</b>
        <button type="button" class="mini danger">Close</button>
      </div>
      <div class="web-vtk-modal-body"></div>
    </div>
  `;
  const closeBtn = overlay.querySelector('button');
  closeBtn.onclick = () => overlay.remove();
  overlay.addEventListener('click', (ev) => {
    if (ev.target === overlay) overlay.remove();
  });
  document.body.appendChild(overlay);
  const body = overlay.querySelector('.web-vtk-modal-body');
  initWebVTKViewer(body, preview, { opacity: 1 });
}

function renderWebVTKViewer(preview, opts={}) {
  const wrap = document.createElement('div');
  wrap.className = 'web-vtk-viewer-wrap';

  const toolbar = document.createElement('div');
  toolbar.className = 'web-vtk-toolbar';

  const label = document.createElement('span');
  label.className = 'web-vtk-label';
  label.textContent = preview.web_viewer_label || 'Web 3D Viewer';
  toolbar.appendChild(label);

  const fullBtn = document.createElement('button');
  fullBtn.type = 'button';
  fullBtn.className = 'mini';
  fullBtn.textContent = 'Fullscreen';
  fullBtn.onclick = () => openWebViewerModal(preview);
  toolbar.appendChild(fullBtn);

  if (preview.download_url) {
    const dl = document.createElement('a');
    dl.className = 'download mini';
    dl.href = preview.download_url;
    dl.target = '_blank';
    dl.textContent = 'Download mesh';
    toolbar.appendChild(dl);
  }

  wrap.appendChild(toolbar);

  const viewer = document.createElement('div');
  viewer.className = opts.inline ? 'web-vtk-viewer inline' : 'web-vtk-viewer';
  wrap.appendChild(viewer);

  setTimeout(() => initWebVTKViewer(viewer, preview), 0);
  return wrap;
}



function previewScalarOptions(preview) {
  const opts = [];
  const add = (name, association) => {
    if (!name) return;
    const key = `${association}:${name}`;
    if (opts.some(o => o.key === key || o.name === name)) return;
    opts.push({ key, name: String(name), association });
  };
  for (const name of preview?.cell_data || []) add(name, 'cell');
  for (const name of preview?.point_data || []) add(name, 'point');

  // Inline web viewer previews expose richer scalarArrays in some cases.
  for (const a of preview?.scalarArrays || []) {
    add(a.name, a.association || 'scalar');
  }

  // Make sure the currently configured scalar is available even when the mesh
  // metadata was compacted.
  if (preview?.web_scalar) add(preview.web_scalar, 'selected');
  if (preview?.scalar_used) add(preview.scalar_used, 'selected');

  return opts;
}

function urlWithQueryParam(url, key, value) {
  if (!url) return url;
  try {
    const u = new URL(url, window.location.origin);
    if (value === null || value === undefined || value === '') {
      u.searchParams.delete(key);
    } else {
      u.searchParams.set(key, value);
    }
    return `${u.pathname}${u.search}${u.hash || ''}`;
  } catch {
    const sep = url.includes('?') ? '&' : '?';
    return value ? `${url}${sep}${encodeURIComponent(key)}=${encodeURIComponent(value)}` : url;
  }
}

function selectedPyVistaUrl(preview, scalarName) {
  let url = preview.pyvista_preview_url || preview.mesh_preview_url || '';
  if (!url) return url;
  // Only mesh preview supports the scalars query argument. Other popups, such
  // as GemPy model or table preview, keep their original URL.
  if (!url.includes('/api/pyvista/mesh/')) return url;
  return urlWithQueryParam(url, 'scalars', scalarName || '');
}


function renderPreview(preview, options={}) {
  const opts = {
    mode: options.mode || 'result',
    showActions: options.showActions !== false,
    compactJson: options.compactJson === true,
  };

  const wrap = document.createElement('div');
  wrap.className = `preview-wrap preview-${opts.mode}`;

  if (preview == null) {
    wrap.innerHTML = '<pre>null</pre>';
    return wrap;
  }

  const addActions = () => {
    if (!opts.showActions) return;

    const actions = document.createElement('div');
    actions.className = 'preview-actions';

    if (preview.pyvista_preview_url) {
      const scalarOptions = previewScalarOptions(preview);
      let scalarSelect = null;

      if (preview.pyvista_preview_url.includes('/api/pyvista/mesh/') && scalarOptions.length) {
        const control = document.createElement('label');
        control.className = 'pyvista-scalar-control';
        control.textContent = 'Open 3D scalar';

        scalarSelect = document.createElement('select');
        scalarSelect.className = 'pyvista-scalar-select';

        const solid = document.createElement('option');
        solid.value = '';
        solid.textContent = 'Solid color';
        scalarSelect.appendChild(solid);

        const preferred = preview.scalar_used || preview.web_scalar || '';
        for (const s of scalarOptions) {
          const opt = document.createElement('option');
          opt.value = s.name;
          opt.textContent = `${s.name}${s.association ? ` (${s.association})` : ''}`;
          if (s.name === preferred) opt.selected = true;
          scalarSelect.appendChild(opt);
        }

        control.appendChild(scalarSelect);
        actions.appendChild(control);
      }

      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'primary';
      btn.textContent = preview.pyvista_button_label || 'Enlarge / Open 3D';
      btn.onclick = () => openPyVista(selectedPyVistaUrl(preview, scalarSelect ? scalarSelect.value : ''), btn);
      actions.appendChild(btn);
    }

    if (preview.download_url) {
      const a = document.createElement('a');
      a.className = 'download secondary-download';
      a.href = preview.download_url;
      a.target = '_blank';
      a.textContent = `Download ${preview.file_name || preview.name || 'data'}`;
      actions.appendChild(a);
    }

    if (actions.children.length) wrap.appendChild(actions);

    if (preview.pyvista_note) {
      const note = document.createElement('div');
      note.className = 'param-help inline-help';
      note.textContent = preview.pyvista_note;
      wrap.appendChild(note);
    }
  };

  if (preview.web_mesh_url) {
    wrap.appendChild(renderWebVTKViewer(preview, { inline: opts.mode === 'inline' }));
  } else if (preview.image_url) {
    const img = document.createElement('img');
    img.className = opts.mode === 'inline' ? 'inline-plot-preview' : 'plot-preview';
    img.src = preview.image_url;
    img.alt = preview.file_name || preview.name || 'preview image';
    wrap.appendChild(img);
  }

  addActions();

  if (preview.pyvista_available === false && preview.pyvista_reason) {
    const note = document.createElement('div');
    note.className = 'param-help';
    note.textContent = preview.pyvista_reason;
    wrap.appendChild(note);
  }

  if (!preview.web_mesh_url && !preview.image_url && opts.mode === 'inline' && preview.pyvista_preview_url) {
    const card = document.createElement('div');
    card.className = 'inline-3d-placeholder';
    const title = preview.spatial_preview_available ? 'Spatial table point cloud' : (preview.name || preview.file_name || preview.preview_type || '3D preview');
    card.innerHTML = `<div class="placeholder-title">${escapeHtml(title)}</div><div class="placeholder-subtitle">Inline WebGL preview is unavailable for this data. Use Enlarge / Open 3D for interactive PyVista viewing.</div>`;
    wrap.insertBefore(card, wrap.firstChild);
  }

  if (opts.mode === 'inline' && preview.spatial_preview_available === false && preview.spatial_preview_reason) {
    const note = document.createElement('div');
    note.className = 'param-help';
    note.textContent = preview.spatial_preview_reason;
    wrap.appendChild(note);
  }

  if (preview.thumbnail_error && opts.mode === 'inline') {
    const note = document.createElement('div');
    note.className = 'param-help';
    note.textContent = `Inline thumbnail unavailable: ${preview.thumbnail_error}`;
    wrap.appendChild(note);
  }

  if (preview.head && Array.isArray(preview.head)) {
    const columns = preview.columns || Object.keys(preview.head[0] || {});
    const table = document.createElement('table');
    table.className = 'preview';
    table.innerHTML = `<thead><tr>${columns.map(c => `<th>${escapeHtml(c)}</th>`).join('')}</tr></thead>`;
    const tbody = document.createElement('tbody');
    for (const row of preview.head) {
      const tr = document.createElement('tr');
      tr.innerHTML = columns.map(c => `<td>${escapeHtml(row[c])}</td>`).join('');
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);

    const summary = document.createElement('pre');
    const reduced = { ...preview };
    delete reduced.head;
    delete reduced.image_url;
    delete reduced.pyvista_preview_url;
    delete reduced.download_url;
    summary.textContent = JSON.stringify(reduced, null, 2);
    wrap.appendChild(summary);
    wrap.appendChild(table);
    return wrap;
  }

  const reduced = { ...preview };
  if (opts.compactJson || preview.web_mesh_url) {
    delete reduced.image_url;
    delete reduced.pyvista_preview_url;
    delete reduced.download_url;
    delete reduced.web_mesh_url;
    delete reduced.mesh_preview_url;
  }

  const pre = document.createElement('pre');
  pre.textContent = typeof reduced === 'string' ? reduced : JSON.stringify(reduced, null, 2);
  wrap.appendChild(pre);
  return wrap;
}


async function openPyVista(url, btn) {
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Starting...';
  try {
    const out = await api(url);
    hint.textContent = out.message || 'PyVista preview started.';
    hint.classList.remove('hidden');
    setTimeout(() => hint.classList.add('hidden'), 3500);
  } catch (err) {
    alert(String(err));
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

async function clearExecutionCache() {
  try {
    const out = await api('/api/cache/clear', { method: 'POST' });
    state.nodeResults = {};
    state.nodeStatuses = {};
    state.runLog = [];
    state.lastManifest = null;
    state.lastExecution = null;
    state.lastRunScope = null;
    renderResultsForSelected();
    renderInspector();
    renderNodes();
    renderRunLog();
    hint.textContent = `Execution cache cleared (${out.removed || 0} cached node result${(out.removed || 0) === 1 ? '' : 's'} removed).`;
    hint.classList.remove('hidden');
    setTimeout(() => hint.classList.add('hidden'), 2800);
  } catch (err) {
    alert(String(err));
  }
}

async function uploadFile(ev) {
  ev.preventDefault();
  const file = el('uploadInput').files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append('file', file);
  fd.append('category', el('uploadCategoryInput')?.value || 'Uncategorized');
  const btn = ev.target.querySelector('button');
  btn.disabled = true;
  btn.textContent = 'Uploading...';
  try {
    await fetch('/api/upload', { method: 'POST', body: fd }).then(async r => {
      if (!r.ok) throw new Error(await r.text());
      return r.json();
    });
    el('uploadInput').value = '';
    await refreshUploads();
  } catch (err) {
    alert(String(err));
  } finally {
    btn.disabled = false;
    btn.textContent = 'Upload';
  }
}


function downloadBlobFallback(filename, blob) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

async function saveBlobAs(filename, blob, options={}) {
  // Chrome/Edge on localhost support the File System Access API, which opens a
  // native "Save As" dialog. Firefox/Safari or non-secure contexts fall back to
  // the browser download behavior.
  if (window.showSaveFilePicker) {
    try {
      const handle = await window.showSaveFilePicker({
        suggestedName: filename,
        types: options.types || [],
      });
      const writable = await handle.createWritable();
      await writable.write(blob);
      await writable.close();
      return { method: 'file-picker' };
    } catch (err) {
      // AbortError means the user canceled the Save As dialog. Do not trigger
      // a fallback download in that case.
      if (err && err.name === 'AbortError') {
        return { method: 'cancelled' };
      }
      console.warn('Save picker failed; falling back to download.', err);
    }
  }
  downloadBlobFallback(filename, blob);
  return { method: 'download-fallback' };
}

async function downloadJson(filename, data) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  return await saveBlobAs(filename, blob, {
    types: [{
      description: 'JSON file',
      accept: { 'application/json': ['.json'] },
    }],
  });
}

async function saveProjectJson() {
  const project = {
    schema_version: 'gempy-node-editor-project-v18',
    saved_at: new Date().toISOString(),
    graph: graphPayload(),
    uploads: state.uploads,
    last_manifest: state.lastManifest,
    last_run_log: state.runLog,
    node_statuses: state.nodeStatuses,
    custom_workflows: state.workflowTemplates || [],
  };
  await downloadJson('gempy_node_project.json', project);
}


async function savePortableProjectZip() {
  const project = {
    schema_version: 'gempy-node-editor-project-v19',
    saved_at: new Date().toISOString(),
    graph: graphPayload(),
    uploads: state.uploads,
    last_manifest: state.lastManifest,
    last_run_log: state.runLog,
    node_statuses: state.nodeStatuses,
    custom_workflows: state.workflowTemplates || [],
  };
  try {
    const res = await fetch('/api/project/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(project),
    });
    if (!res.ok) throw new Error(await res.text());
    const blob = await res.blob();
    await saveBlobAs('gempy_node_portable_project.zip', blob, {
      types: [{
        description: 'GemPy portable project ZIP',
        accept: { 'application/zip': ['.zip'] },
      }],
    });
  } catch (err) {
    alert(String(err));
  }
}

async function loadPortableProjectZip(file) {
  if (!file) return;
  const fd = new FormData();
  fd.append('file', file);
  try {
    const res = await fetch('/api/project/import', { method: 'POST', body: fd });
    if (!res.ok) throw new Error(await res.text());
    const out = await res.json();
    const project = out.project || {};
    applyLoadedProject(project, out);
    await refreshUploads();
    renderAll();
    hint.textContent = `Portable project imported. Files imported: ${(out.imported_files || []).length}.`;
    hint.classList.remove('hidden');
    setTimeout(() => hint.classList.add('hidden'), 3500);
  } catch (err) {
    alert(String(err));
  }
}

async function saveRunManifest() {
  if (!state.lastManifest) {
    alert('No run manifest available. Run a graph first.');
    return;
  }
  await downloadJson(`gempy_run_manifest_${state.lastManifest.run_id || 'latest'}.json`, state.lastManifest);
}

function renderRunLog() {
  const root = el('runLog');
  if (!root) return;
  const rows = state.runLog || [];
  if (!rows.length) {
    root.className = 'run-log empty';
    root.textContent = 'No run yet.';
    return;
  }
  root.className = 'run-log';
  const runId = state.lastExecution?.run_id || state.lastManifest?.run_id || '';
  const graphHash = state.lastExecution?.graph_hash || state.lastManifest?.graph_hash || '';
  root.innerHTML = `<div class="run-log-meta"><b>Run</b> ${escapeHtml(runId)}<br><b>Graph hash</b> ${escapeHtml(String(graphHash).slice(0, 16))}${graphHash ? '…' : ''}</div>`;
  const table = document.createElement('table');
  table.className = 'run-log-table';
  table.innerHTML = '<thead><tr><th>Status</th><th>Node</th><th>Time</th></tr></thead>';
  const tbody = document.createElement('tbody');
  for (const row of rows) {
    const tr = document.createElement('tr');
    tr.className = row.status || '';
    const status = row.status || '';
    const dur = row.duration_ms !== undefined ? `${row.duration_ms} ms` : '';
    tr.innerHTML = `<td>${escapeHtml(status)}</td><td title="${escapeHtml(row.node_id || '')}">${escapeHtml(row.title || row.type || row.node_id || '')}</td><td>${escapeHtml(dur)}</td>`;
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  root.appendChild(table);
}

function saveGraphJson() {
  const data = JSON.stringify(graphPayload(), null, 2);
  const blob = new Blob([data], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'gempy_node_graph.json';
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function loadGraphJson(file) {
  const reader = new FileReader();
  reader.onload = () => {
    try {
      const raw = JSON.parse(reader.result);
      const g = raw.graph || raw;
      state.nodes = g.nodes || [];
      state.edges = g.edges || [];
      state.selectedNodeId = null;
      state.lastExecution = null;
      state.nodeResults = {};
      state.nodeStatuses = raw.node_statuses || {};
      state.runLog = raw.last_run_log || [];
      state.lastManifest = raw.last_manifest || null;
      state.nodeCounter = state.nodes.length + 1;
      renderAll();
    } catch (err) {
      alert(`Invalid graph JSON: ${err}`);
    }
  };
  reader.readAsText(file);
}

function clearGraph() {
  if (!confirm('Clear all nodes and edges?')) return;
  state.nodes = [];
  state.edges = [];
  state.selectedNodeId = null;
  state.pendingConnection = null;
  state.lastExecution = null;
  state.nodeResults = {};
  state.nodeStatuses = {};
  state.runLog = [];
  state.lastManifest = null;
  el('results').className = 'results empty';
  el('results').textContent = 'Select a node, then run the selected chain.';
  renderAll();
}

function handleKeydown(ev) {
  const active = document.activeElement;
  const tag = active?.tagName?.toLowerCase();
  const editing = active?.isContentEditable || ['input', 'textarea', 'select'].includes(tag);
  if (editing) return;
  if ((ev.key === 'Delete' || ev.key === 'Backspace') && state.selectedNodeId) {
    ev.preventDefault();
    deleteNode(state.selectedNodeId);
  }
  if (ev.key === 'Escape' && state.pendingConnection) {
    ev.preventDefault();
    endConnection('Connection cancelled.');
  }
}

function escapeHtml(v) {
  if (v === null || v === undefined) return '';
  return String(v).replace(/[&<>"']/g, s => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[s]));
}

function cssEscape(v) {
  if (window.CSS && CSS.escape) return CSS.escape(v);
  return String(v).replace(/[^a-zA-Z0-9_-]/g, '_');
}

el('uploadForm').addEventListener('submit', uploadFile);
el('btnRun').addEventListener('click', () => runGraph('selected'));
el('btnClearCache').addEventListener('click', clearExecutionCache);
el('btnRunAll').addEventListener('click', () => runGraph('all'));
el('btnStopRun').addEventListener('click', stopCurrentRun);
el('btnSaveGraph').addEventListener('click', saveGraphJson);
el('btnSaveProject').addEventListener('click', saveProjectJson);
el('btnSavePortableProject').addEventListener('click', savePortableProjectZip);
el('btnSaveManifest').addEventListener('click', saveRunManifest);
el('btnClear').addEventListener('click', clearGraph);
el('btnRefreshFiles').addEventListener('click', refreshUploads);
el('btnCleanupFiles').addEventListener('click', cleanupMissingFiles);
el('graphFileInput').addEventListener('change', ev => {
  const f = ev.target.files[0];
  if (f) loadGraphJson(f);
  ev.target.value = '';
});
el('portableProjectInput').addEventListener('change', ev => {
  const f = ev.target.files[0];
  if (f) loadPortableProjectZip(f);
  ev.target.value = '';
});
canvas.addEventListener('dragover', ev => {
  if (ev.dataTransfer.types.includes('application/x-gempy-upload-file-id') || ev.dataTransfer.types.includes('text/plain')) {
    ev.preventDefault();
    ev.dataTransfer.dropEffect = 'copy';
  }
});
canvas.addEventListener('drop', ev => {
  const fileId = ev.dataTransfer.getData('application/x-gempy-upload-file-id') || ev.dataTransfer.getData('text/plain');
  if (!fileId) return;
  const file = (state.uploads || []).find(f => f.file_id === fileId);
  if (!file) return;
  ev.preventDefault();
  addLoadUploadedFileNode(file, canvasPositionFromClient(ev));
});
canvas.addEventListener('mousedown', startPan);
canvas.addEventListener('scroll', renderEdges);
window.addEventListener('resize', renderEdges);
document.addEventListener('keydown', handleKeydown);

init().catch(err => {
  console.error(err);
  alert(String(err));
});



// Inspector controls should not start canvas/node drag actions.
document.addEventListener('pointerdown', (event) => {
  const inspector = document.getElementById('inspector');
  if (inspector && inspector.contains(event.target)) {
    event.stopPropagation();
  }
}, true);
