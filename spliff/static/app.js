const get = id => document.getElementById(id);
const element = (tag, content, className) => {
  const node = document.createElement(tag);
  node.textContent = content;
  if (className) node.className = className;
  return node;
};

async function api(url, options) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Catalog unavailable');
  return data;
}

function encoding(track) {
  return [track.codec?.toUpperCase(), track.bitrate && Math.round(track.bitrate / 1000) + ' kbps',
    track.sample_rate && track.sample_rate / 1000 + ' kHz', track.bits && track.bits + ' bit']
    .filter(Boolean).join(' · ') || 'Unknown';
}

function trackRow(track) {
  const row = document.createElement('tr');
  const name = document.createElement('td');
  name.append(element('strong', track.title || track.path.split('/').pop()),
    element('small', [track.artist, '#' + track.id, track.path].filter(Boolean).join(' · '), 'muted'));
  const status = [track.protected && 'Protected', !track.present && 'Missing', track.error]
    .filter(Boolean).join(' · ') || 'Indexed';
  const actions = element('td', '', 'track-actions');
  const protection = element('button', track.protected ? 'Unprotect' : 'Protect');
  protection.addEventListener('click', async () => {
    protection.disabled = true;
    try {
      const result = await action('/api/tracks/' + track.id + (track.protected ? '/unprotect' : '/protect'));
      notice(result.protected ? 'Track protected.' : 'Protection removed.');
      refreshSummary();
      loadTracks();
    } catch (error) {
      notice(error.message);
      protection.disabled = false;
    }
  });
  actions.append(protection);
  if (track.present && !track.error) {
    const compare = element('button', 'Compare file');
    compare.addEventListener('click', () => chooseCandidate(track));
    const match = element('button', 'Compare with download');
    match.addEventListener('click', () => {
      downloadOriginal = track;
      view('downloads');
    });
    actions.append(compare, match);
  }
  row.append(name, element('td', track.album || '—'), element('td', encoding(track)), element('td', status), actions);
  return row;
}

let cursor = null;
let currentSearch = new URLSearchParams();
let requestNumber = 0;
async function loadTracks(reset = true) {
  const request = ++requestNumber;
  if (reset) {
    cursor = null;
    currentSearch = new URLSearchParams({q: get('search').value, scope: get('scope').value});
    get('rows').replaceChildren();
    get('table').hidden = true;
  }
  const query = new URLSearchParams(currentSearch);
  query.set('after', cursor || 0);
  get('more').hidden = true;
  get('status').textContent = 'Loading tracks…';
  try {
    const data = await api('/api/tracks?' + query);
    if (request !== requestNumber) return;
    get('rows').append(...data.items.map(trackRow));
    get('table').hidden = get('rows').childElementCount === 0;
    cursor = data.next;
    get('more').hidden = cursor === null;
    const shown = get('rows').childElementCount;
    get('status').textContent = shown
      ? shown.toLocaleString() + (shown === 1 ? ' track shown' : ' tracks shown')
      : 'No tracks match this view.';
  } catch (error) {
    if (request !== requestNumber) return;
    get('status').textContent = error.message;
    get('more').hidden = cursor === null;
  }
}

function side(label, track, fallback) {
  const section = document.createElement('section');
  section.append(element('h3', label), element('p', track.title || fallback),
    element('p', [track.artist, track.album].filter(Boolean).join(' · ') || 'No artist or album tag', 'muted'),
    element('p', encoding(track) + (track.duration ? ' · ' + track.duration.toFixed(1) + ' s' : ''), 'muted'));
  return section;
}

async function loadReviews() {
  get('review-status').textContent = 'Loading staged reviews…';
  try {
    const {items} = await api('/api/reviews');
    get('reviews').replaceChildren(...items.map(item => {
      const card = element('article', '', 'review');
      const compare = element('div', '', 'comparison');
      const original = item.original_snapshot;
      const status = [item.protected && 'Protected original', !item.present && 'Original missing']
        .filter(Boolean).join(' · ');
      compare.append(side('Original', original, original.path), side('Candidate', item.snapshot, item.candidate));
      card.append(element('h2', 'Review #' + item.id + ' · Track #' + item.original_id),
        element('small', status || 'Awaiting manual comparison', 'muted'), compare);
      return card;
    }));
    get('review-status').textContent = items.length
      ? 'Showing saved observations. Recording identity and fidelity require manual review.'
      : 'No staged comparisons. Choose Compare file beside a track to start.';
  } catch (error) {
    get('review-status').textContent = error.message;
  }
}

function view(page) {
  document.body.dataset.view = page;
  for (const [id, key, panel] of [['browse', 'tracks', 'index'],
    ['staged', 'reviews', 'review-panel'], ['downloads', 'downloads', 'downloads-panel']]) {
    get(id).setAttribute('aria-pressed', page === key);
    get(panel).hidden = page !== key;
  }
  if (page === 'reviews') loadReviews();
  if (page === 'downloads') loadDownloads();
  else { downloadRequest++; downloadListRequest++; clearTimeout(downloadTimer); }
}

let downloadOriginal = null;
let downloadRequest = 0;
let downloadTimer;
let downloadListRequest = 0;
let selectedDownload = null;
let downloadJobs = [];
let keptComparison = null;

function selectDownload(job) {
  if (selectedDownload?.id !== job.id) {
    get('download-report').replaceChildren();
    get('download-original').textContent = '';
    get('download-comparison').hidden = true;
  }
  selectedDownload = job;
  get('download-heading').hidden = false;
  get('download-title').textContent = job.title;
  get('download-source').textContent = job.peer;
  get('download-progress').max = job.total;
  get('download-progress').value = job.total;
  for (const button of get('download-list').querySelectorAll('button')) {
    button.setAttribute('aria-pressed', button.dataset.job === job.id);
  }
  refreshDownload();
}

async function loadDownloads(selected, quiet = false) {
  const request = ++downloadListRequest;
  clearTimeout(downloadTimer);
  try {
    const {items} = await api('/api/downloads');
    if (request !== downloadListRequest) return;
    if (quiet && items.map(item => item.id).join() === downloadJobs.map(item => item.id).join()) return;
    downloadJobs = items;
    const list = get('download-list');
    list.replaceChildren(...items.map(job => {
      const button = element('button', '', 'download-job');
      button.dataset.job = job.id;
      button.append(element('strong', job.title), element('small', job.peer + ' · ' + job.total + ' tracks'));
      button.addEventListener('click', () => {
        downloadOriginal = null;
        selectDownload(job);
      });
      return button;
    }));
    const job = items.find(item => item.id === (selected || selectedDownload?.id)) || items[0];
    if (job) selectDownload(job);
    else {
      selectedDownload = null;
      downloadRequest++;
      get('download-heading').hidden = true;
      get('download-comparison').hidden = true;
      get('download-report').replaceChildren();
      get('download-status').textContent = 'No completed downloads yet.';
    }
  } catch (error) {
    if (request === downloadListRequest && !quiet) get('download-status').textContent = error.message;
  } finally {
    if (request === downloadListRequest && !get('downloads-panel').hidden) {
      downloadTimer = setTimeout(() => loadDownloads(null, true), 8000);
    }
  }
}

function showDownload(result, job) {
  const review = result.items;
  const matched = review.filter(item => item.assessment === 'possible match').length;
  const count = result.complete + ' of ' + review.length + ' tracks downloaded';
  const progress = get('download-progress');
  progress.value = result.complete;
  get('download-status').textContent = count + (result.comparison && !result.comparison.album_mismatch ? ' · ' + matched + ' possible matches' : '');
  const candidateAlbum = review.find(item => item.audio?.album)?.audio.album;
  get('download-title').textContent = candidateAlbum || job.title;
  get('download-comparison').hidden = !result.complete && !downloadOriginal;
  const kept = result.kept_both;
  get('download-original').textContent = kept ? 'Both versions saved' :
    result.comparison?.album || downloadOriginal?.title || 'No album selected';
  get('choose-original').textContent = 'Choose comparison album';
  get('choose-original').hidden = Boolean(kept);
  const eligible = !kept && result.kind === 'album' && result.complete === review.length &&
    matched === review.length && !result.originals_without_match.length &&
    review.every(item => item.audio?.codec === 'flac' && item.original?.codec === 'mp3' &&
      !item.original.protected);
  get('keep-both').hidden = !eligible;
  keptComparison = eligible ? {candidate: job.id, original: result.comparison.original_track_id} : null;
  const rows = review.map(item => {
    const row = document.createElement('tr');
    const title = element('td', item.audio?.title || item.name);
    if (item.audio) title.append(element('small', encoding(item.audio), 'muted'));
    const original = element('td', item.original?.title || '—');
    if (item.original) {
      original.append(element('small', [encoding(item.original), item.original.protected && 'Protected']
        .filter(Boolean).join(' · '), 'muted'));
    }
    const status = item.assessment === 'original not selected' ? item.status : item.assessment || item.status;
    row.append(title, original, element('td', status.charAt(0).toUpperCase() + status.slice(1)));
    return row;
  });
  const table = document.createElement('table');
  const header = document.createElement('tr');
  for (const label of ['Download', 'In your library', 'Status']) {
    const cell = element('th', label);
    cell.scope = 'col';
    header.append(cell);
  }
  table.createTHead().append(header);
  table.createTBody().append(...rows);
  const wrap = element('div', '', 'table-wrap');
  wrap.append(table);
  const report = get('download-report');
  report.replaceChildren(wrap);
  if (kept) report.prepend(element('p', 'FLACs are in the album folder. Original MP3s are in its MP3 subfolder.', 'message'));
  if (candidateAlbum && result.comparison?.album_mismatch) {
    report.prepend(element('p', 'This download is ' + candidateAlbum + '. Choose that album in your library to compare it.', 'message'));
  }
  const missing = result.originals_without_match.map(track => track.title || track.path);
  if (missing.length) {
    report.append(element('p', 'Original tracks to review: ' + missing.join(', '), 'message'));
  }
}

async function refreshDownload() {
  const job = selectedDownload;
  if (!job || get('downloads-panel').hidden) return;
  const request = ++downloadRequest;
  const query = new URLSearchParams({candidate: job.id});
  if (downloadOriginal) query.set('original', downloadOriginal.id);
  if (!get('download-report').childElementCount) get('download-status').textContent = 'Checking download…';
  try {
    const result = await api('/api/download-review?' + query);
    if (request !== downloadRequest) return;
    if (result.complete !== result.items.length) {
      get('download-list').querySelector('[aria-pressed="true"]')?.remove();
      selectedDownload = null;
      get('download-heading').hidden = true;
      get('download-comparison').hidden = true;
      get('download-report').replaceChildren();
      get('download-status').textContent = '';
      return;
    }
    showDownload(result, job);
  } catch (error) {
    if (request === downloadRequest) get('download-status').textContent = error.message;
  }
}

get('queue-form').addEventListener('submit', async event => {
  event.preventDefault();
  const submit = event.currentTarget.querySelector('button');
  submit.disabled = true;
  const input = get('download-code');
  get('queue-status').textContent = 'Sending your selection to Nicotine+…';
  try {
    const result = await action('/api/downloads?' + new URLSearchParams({candidate: input.value.trim()}));
    input.value = '';
    get('queue-status').textContent = 'Sent to Nicotine+. It will appear here when complete.';
    await loadDownloads(result.id, true);
  } catch (error) {
    get('queue-status').textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

const keepDialog = get('keep-both-dialog');
const keepStatus = get('keep-both-status');
const keepConfirm = get('confirm-keep-both');
let keepParameters = null;
let keepPreviewRequest = 0;
let applyingKeepBoth = false;

get('keep-both').addEventListener('click', async () => {
  if (!keptComparison) return;
  const request = ++keepPreviewRequest;
  const query = new URLSearchParams(keptComparison);
  keepParameters = null;
  keepConfirm.disabled = true;
  get('keep-both-summary').textContent = '';
  get('flac-destination').textContent = '';
  get('mp3-destination').textContent = '';
  keepStatus.textContent = 'Checking files and destinations…';
  keepDialog.showModal();
  try {
    const plan = await api('/api/keep-both?' + query);
    if (!keepDialog.open || request !== keepPreviewRequest) return;
    get('keep-both-summary').textContent =
      'Copy ' + plan.count + ' FLACs into ' + plan.album + ' and move the matching MP3s into MP3.';
    get('flac-destination').textContent = plan.destination;
    get('mp3-destination').textContent = plan.archive;
    keepStatus.textContent = '';
    query.set('token', plan.token);
    keepParameters = query;
    keepConfirm.disabled = false;
  } catch (error) {
    if (request === keepPreviewRequest) keepStatus.textContent = error.message;
  }
});

get('cancel-keep-both').addEventListener('click', () => keepDialog.close());
keepDialog.addEventListener('cancel', event => { if (applyingKeepBoth) event.preventDefault(); });
keepConfirm.addEventListener('click', async () => {
  if (!keepParameters || applyingKeepBoth) return;
  applyingKeepBoth = true;
  keepConfirm.disabled = true;
  get('cancel-keep-both').disabled = true;
  keepStatus.textContent = 'Saving both versions…';
  try {
    await action('/api/keep-both?' + keepParameters);
    keepDialog.close();
    notice('Both versions saved. Original MP3s are in the album’s MP3 folder.');
    refreshSummary();
    loadTracks();
    refreshDownload();
  } catch (error) {
    keepStatus.textContent = error.message;
  } finally {
    applyingKeepBoth = false;
    get('cancel-keep-both').disabled = false;
  }
});

get('reload-download').addEventListener('click', refreshDownload);
get('choose-original').addEventListener('click', () => {
  const job = downloadJobs.find(item => item.id === selectedDownload?.id);
  get('search').value = job?.kind === 'album' ? get('download-title').textContent : '';
  get('scope').value = 'all';
  view('tracks');
  loadTracks();
  get('search').focus();
});

get('filters').addEventListener('submit', event => { event.preventDefault(); loadTracks(); });
get('scope').addEventListener('change', () => loadTracks());
get('more').addEventListener('click', () => loadTracks(false));
get('browse').addEventListener('click', () => view('tracks'));
get('staged').addEventListener('click', () => view('reviews'));
get('downloads').addEventListener('click', () => view('downloads'));
function refreshSummary() {
  api('/api/summary').then(data => {
    for (const key of ['tracks', 'lossless', 'lossy', 'protected']) get(key).textContent = data[key].toLocaleString();
  }).catch(() => {
    for (const key of ['tracks', 'lossless', 'lossy', 'protected']) get(key).textContent = 'Unavailable';
  });
}

function action(url, body = '', headers = {}) {
  return api(url, {method: 'POST', body, headers: {'X-Spliff-Action': 'dashboard', ...headers}});
}

function notice(message) {
  get('action-status').textContent = message;
  get('action-status').hidden = false;
}

let original = null;
let uploading = false;
const dialog = get('candidate-dialog');
const dropZone = get('drop-zone');
const picker = get('candidate-file');

function chooseCandidate(track) {
  original = track;
  get('candidate-original').textContent = 'Original: ' + (track.title || track.path);
  get('candidate-status').textContent = '';
  picker.value = '';
  dialog.showModal();
}

async function compareFiles(files) {
  if (uploading) return;
  const status = get('candidate-status');
  if (files.length !== 1) {
    status.textContent = 'Choose one candidate file.';
    return;
  }
  const file = files[0];
  if (file.size > 512 * 1024 * 1024) {
    status.textContent = 'Choose a candidate under 512 MB.';
    return;
  }
  uploading = true;
  get('choose-candidate').disabled = true;
  get('close-candidate').disabled = true;
  status.textContent = 'Inspecting candidate…';
  try {
    const review = await action('/api/tracks/' + original.id + '/stage', file, {
      'X-Spliff-Filename': encodeURIComponent(file.name)
    });
    dialog.close();
    notice('Saved review #' + review.id + '. Compare the original and candidate below.');
    view('reviews');
    get('staged').focus();
  } catch (error) {
    status.textContent = error.message;
  } finally {
    uploading = false;
    get('choose-candidate').disabled = false;
    get('close-candidate').disabled = false;
    picker.value = '';
  }
}

get('close-candidate').addEventListener('click', () => dialog.close());
dialog.addEventListener('cancel', event => { if (uploading) event.preventDefault(); });
get('choose-candidate').addEventListener('click', () => picker.click());
picker.addEventListener('change', () => { if (picker.files.length) compareFiles(picker.files); });
dropZone.addEventListener('dragover', event => {
  event.preventDefault();
  dropZone.classList.add('dragging');
});
dropZone.addEventListener('dragleave', event => {
  if (!dropZone.contains(event.relatedTarget)) dropZone.classList.remove('dragging');
});
dropZone.addEventListener('drop', event => {
  event.preventDefault();
  dropZone.classList.remove('dragging');
  compareFiles(event.dataTransfer.files);
});
document.addEventListener('dragover', event => {
  if (event.dataTransfer.types.includes('Files')) event.preventDefault();
});
document.addEventListener('drop', event => {
  if (event.dataTransfer.types.includes('Files')) event.preventDefault();
});

get('scan').addEventListener('click', async () => {
  get('scan').disabled = true;
  notice('Choose a folder in the macOS picker. Spliff will scan it after you confirm.');
  try {
    const result = await action('/api/scan');
    if (result.cancelled) {
      notice('Folder selection canceled.');
    } else {
      notice('Scan complete. ' + result.probed + ' inspected, ' + result.cached + ' already indexed, ' +
        result.errors + ' inspection errors.');
      refreshSummary();
      loadTracks();
    }
  } catch (error) {
    notice(error.message);
  } finally {
    get('scan').disabled = false;
  }
});

refreshSummary();
loadTracks();
api('/api/downloads').then(({items}) => {
  if (items.length && !get('search').value && !get('index').hidden
      && document.activeElement === document.body) view('downloads');
}).catch(() => {});
