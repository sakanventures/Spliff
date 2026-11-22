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
    const match = element('button', 'Match download');
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
  for (const [id, key, panel] of [['browse', 'tracks', 'index'],
    ['staged', 'reviews', 'review-panel'], ['downloads', 'downloads', 'downloads-panel']]) {
    get(id).setAttribute('aria-pressed', page === key);
    get(panel).hidden = page !== key;
  }
  if (page === 'reviews') loadReviews();
  if (page === 'downloads') loadDownloads();
}

let downloadOriginal = null;
let downloadRequest = 0;

async function loadDownloads(selected) {
  const request = ++downloadRequest;
  const picker = get('download-picker');
  const previous = selected || picker.value;
  get('download-report').replaceChildren();
  get('download-original').textContent = downloadOriginal
    ? 'Compare against ' + (downloadOriginal.album || downloadOriginal.title || downloadOriginal.path) + '.'
    : 'To compare with your library, use Match download on one original track.';
  try {
    const {items} = await api('/api/downloads');
    if (request !== downloadRequest) return;
    picker.replaceChildren(...items.map(item => {
      const option = element('option', item.query + ' · ' + item.id.slice(-10));
      option.value = item.id;
      return option;
    }));
    if (items.some(item => item.id === previous)) picker.value = previous;
    get('download-status').textContent = items.length ? '' : 'No staged downloads yet.';
  } catch (error) {
    if (request === downloadRequest) get('download-status').textContent = error.message;
  }
}

get('queue-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector('button');
  submit.disabled = true;
  const input = get('download-code');
  get('download-status').textContent = 'Queueing the selected candidate in Nicotine+…';
  try {
    const result = await action('/api/downloads?' + new URLSearchParams({candidate: input.value.trim()}));
    input.value = '';
    await loadDownloads(result.id);
    get('download-status').textContent = 'Staging job saved. Select Check download for progress.';
  } catch (error) {
    get('download-status').textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

get('download-review-form').addEventListener('submit', async event => {
  event.preventDefault();
  const request = ++downloadRequest;
  const query = new URLSearchParams({candidate: get('download-picker').value});
  if (downloadOriginal) query.set('original', downloadOriginal.id);
  get('download-status').textContent = 'Checking downloads and inspecting completed files…';
  get('download-report').replaceChildren();
  try {
    const result = await api('/api/download-review?' + query);
    if (request !== downloadRequest) return;
    get('download-status').textContent = result.complete + ' of ' + result.items.length + ' staged files available.';
    const list = document.createElement('ul');
    for (const item of result.items) {
      const details = [item.status, item.assessment, item.audio && encoding(item.audio),
        item.original?.protected && 'original protected'].filter(Boolean);
      const row = element('li', item.name + ' · ' + details.join(' · '));
      if (item.original) {
        row.append(element('small', 'Original: ' + (item.original.title || item.original.path) +
          ' · ' + encoding(item.original), 'muted'));
      }
      list.append(row);
    }
    const missing = result.originals_without_match.map(track => track.title || track.path);
    const report = get('download-report');
    report.append(list);
    if (missing.length) report.append(element('p', 'Originals without a confident metadata match: ' + missing.join(', ')));
  } catch (error) {
    if (request === downloadRequest) get('download-status').textContent = error.message;
  }
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
