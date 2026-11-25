if (new URLSearchParams(location.search).get('embedded') === '1') document.documentElement.classList.add('embedded');
const filePreview = location.protocol === 'file:';
const get = id => document.getElementById(id);
const element = (tag, content, className) => {
  const node = document.createElement(tag);
  node.textContent = content;
  if (className) node.className = className;
  return node;
};

async function api(url, options) {
  if (filePreview) throw new Error('Open the local dashboard to connect to your library.');
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

function candidateDetails(candidate) {
  const bytes = candidate.total_bytes;
  const size = bytes > 0 ? (bytes / (bytes >= 1e9 ? 1e9 : 1e6)).toFixed(1) +
    (bytes >= 1e9 ? ' GB' : ' MB') : 'Size unknown';
  const qualities = (candidate.quality || []).map(quality =>
    (quality.sample_rate ? quality.sample_rate / 1000 + ' kHz' : 'kHz unknown') + ' / ' +
    (quality.bit_depth ? quality.bit_depth + ' bit' : 'bit depth unknown'));
  return size + ' · ' + (qualities.length > 1 ? 'Mixed: ' : '') +
    (qualities.join(', ') || 'Quality unknown') + ' (peer reported)';
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
    const spectrum = element('button', 'Spectrum');
    spectrum.addEventListener('click', () => showTrackSpectrum(track));
    actions.append(spectrum);
    const compare = element('button', 'Compare file');
    compare.addEventListener('click', () => chooseCandidate(track));
    const match = element('button', 'Compare with download');
    match.addEventListener('click', () => {
      downloadOriginal = track;
      view('downloads');
    });
    actions.append(compare, match);
  }
  if (companion) {
    const menu = element('details');
    menu.append(element('summary', 'Actions'), ...actions.children);
    actions.append(menu);
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
  window.webkit?.messageHandlers?.spliffMac?.postMessage("reviewClosed");
  document.body.dataset.view = page;
  get('advanced').open = false;
  for (const [id, key, panel] of [['sync', 'sync', 'sync-panel'], ['albums', 'albums', 'albums-panel'], ['browse', 'tracks', 'index'],
    ['staged', 'reviews', 'review-panel'], ['downloads', 'downloads', 'downloads-panel']]) {
    get(id).setAttribute('aria-pressed', page === key);
    get(panel).hidden = page !== key;
  }
  if (page === 'sync') loadSync();
  else clearTimeout(syncTimer);
  if (page === 'albums' && !get('album-library').hidden) loadAlbums();
  if (page === 'tracks') { refreshSummary(); loadTracks(); }
  if (page === 'reviews') loadReviews();
  if (page === 'downloads') {
    showDownloadList();
    loadDownloads();
  }
  else { downloadRequest++; downloadListRequest++; clearTimeout(downloadTimer); }
}

let downloadOriginal = null;
let downloadRequest = 0;
let downloadTimer;
let downloadListRequest = 0;
let selectedDownload = null;
let downloadJobs = [];
let keptComparison = null;

let albumRequest = 0;
let sourceRequest = 0;
let currentAlbum = null;
let choosingDownloadOriginal = false;
let queueingAlbum = false;

function albumCaption(album) {
  return [album.artist, album.total + (album.total === 1 ? ' track' : ' tracks'),
    album.mp3 === album.total ? 'MP3' : 'Mixed formats', album.protected && 'Protected tracks']
    .filter(Boolean).join(' · ');
}

async function loadAlbums() {
  const request = ++albumRequest;
  const query = get('album-query').value.trim();
  const list = get('album-list');
  const status = get('album-status');
  status.textContent = 'Finding albums…';
  list.replaceChildren();
  try {
    const {items, more, has_library: hasLibrary} = await api('/api/albums?' + new URLSearchParams({q: query}));
    if (request !== albumRequest) return;

    get('album-filter').hidden = !hasLibrary;
    get('library-empty').hidden = hasLibrary;
    status.textContent = !hasLibrary ? '' : !items.length ?
      (query ? 'No albums match your search.' : 'No indexed MP3 albums. Choose a folder to add one.') :
      more ? 'Showing the first 25 albums. Search to narrow the list.' : '';
    list.replaceChildren(...items.map(album => {
      const row = element('li');
      const title = element('div');
      title.append(element('strong', album.album), element('small', albumCaption(album), 'muted'));
      if (items.some(other => other.id !== album.id && other.album.toLowerCase() === album.album.toLowerCase())) {
        title.append(element('small', [album.root, album.folder].filter(Boolean).join('/'), 'muted'));
      }
      const button = element('button', choosingDownloadOriginal ? 'Compare' : 'Find FLACs');
      button.setAttribute('aria-label', button.textContent + ' for ' + album.album);
      button.addEventListener('click', () => {
        if (choosingDownloadOriginal) {
          choosingDownloadOriginal = false;
          downloadOriginal = {id: album.id, title: album.album};
          view('downloads');
        } else {
          openAlbumSearch(album);
        }
      });
      row.append(title, button);
      return row;
    }));
  } catch (error) {
    if (request === albumRequest) status.textContent = error.message;
  }
}

function openAlbumSearch(album) {
  currentAlbum = album;
  get('album-library').hidden = true;
  get('album-search').hidden = false;
  get('selected-album-title').textContent = album.album;
  get('selected-album-info').textContent = albumCaption(album);
  get('soulseek-query').value = (album.album.trim().length < 2 ?
    [album.artist, album.album].filter(Boolean).join(' ') : album.album).slice(0, 180);
  get('soulseek-results').replaceChildren();
  get('soulseek-status').textContent = '';
  get('search-soulseek').disabled = false;
  get('album-back').focus();
  get('soulseek-form').requestSubmit();
}

function showAlbumLibrary(comparing = false) {
  currentAlbum = null;
  sourceRequest++;
  choosingDownloadOriginal = comparing;
  get('albums-heading').textContent = comparing ? 'Which album should Spliff compare?' : companion ? 'Upgrade an album' : 'What album do you want to upgrade?';
  get('album-library').hidden = false;
  get('album-search').hidden = true;
  view('albums');
  if (!get('album-filter').hidden) get('album-query').focus();
}

async function queueAlbum(candidate, album) {
  if (queueingAlbum) return;
  queueingAlbum = true;
  const status = get('soulseek-status');
  const buttons = get('soulseek-results').querySelectorAll('button');
  buttons.forEach(button => { button.disabled = true; });
  status.textContent = 'Sending to Nicotine+…';
  try {
    const params = new URLSearchParams({candidate: candidate.candidate_id, original: album.id});
    await action('/api/downloads?' + params);
    notice('Sent to Nicotine+. The album will appear in Downloads when all files are ready.');
    downloadOriginal = null;
    if (currentAlbum === album && document.body.dataset.view === 'albums') view('downloads');
    status.textContent = 'Sent to Nicotine+.';
  } catch (error) {
    if (currentAlbum === album) status.textContent = error.message;
  } finally {
    buttons.forEach(button => { button.disabled = false; });
    queueingAlbum = false;
  }
}

get('album-filter').addEventListener('submit', event => { event.preventDefault(); loadAlbums(); });
get('album-back').addEventListener('click', () => showAlbumLibrary());
get('soulseek-form').addEventListener('submit', async event => {
  event.preventDefault();
  const request = ++sourceRequest;
  const album = currentAlbum;
  const status = get('soulseek-status');
  const list = get('soulseek-results');
  const searchButton = get('search-soulseek');
  searchButton.disabled = true;
  list.replaceChildren();
  status.textContent = 'Searching Nicotine+…';
  try {
    const {candidates} = await action('/api/album-search?' + new URLSearchParams({q: get('soulseek-query').value}));
    if (request !== sourceRequest) return;
    const options = candidates.filter(candidate => candidate.candidate_id && candidate.flac_files === candidate.matching_files);
    options.sort((first, second) => (second.total_bytes || 0) - (first.total_bytes || 0));
    status.textContent = options.length ? 'Choose a FLAC download. Spliff checks it against your album after it finishes.' :
      'No FLAC albums found. Try adding the artist or changing the search.';
    let visible = 0;
    const moreRow = element('li');
    const more = element('button', 'View more');
    moreRow.append(more);
    const showMore = () => {
      moreRow.remove();
      list.append(...options.slice(visible, visible + 5).map(candidate => {
      const row = element('li');
      const title = element('div');
      title.append(element('strong', candidate.folder.split('\\').pop() || album.album),
        element('small', candidate.flac_files + ' FLAC tracks found · ' + candidate.peer + ' · ' + candidateDetails(candidate), 'muted'));
      const button = element('button', 'Download', 'primary');
      button.setAttribute('aria-label', 'Download FLACs from ' + candidate.peer);
      button.addEventListener('click', () => queueAlbum(candidate, album));
      row.append(title, button);
      return row;
      }));
      visible += 5;
      if (visible < options.length) list.append(moreRow);
    };
    more.addEventListener('click', showMore);
    showMore();
  } catch (error) {
    if (request === sourceRequest) status.textContent = error.message;
  } finally {
    if (request === sourceRequest) searchButton.disabled = false;
  }
});

function showDownloadList() {
  delete get('inline-options').dataset.key;
  window.webkit?.messageHandlers?.spliffMac?.postMessage("reviewClosed");
  get('downloads-panel').classList.remove('show-download');
  get('download-back').hidden = true;
}

get('download-back').addEventListener('click', () => {
  showDownloadList();
  get('download-list').querySelector('[aria-pressed="true"]')?.focus();
});

function selectDownload(job, open = false) {
  if (open) {
    window.webkit?.messageHandlers?.spliffMac?.postMessage('reviewOpen');
    get('action-status').hidden = true;
    get('downloads-panel').classList.add('show-download');
    get('download-back').hidden = false;
    get('download-back').focus();
  }
  if (selectedDownload?.id !== job.id) {
    delete get('inline-options').dataset.key;
    get('download-spectrum').hidden = true;
    get('download-spectrum').replaceChildren();
    delete get('download-spectrum').dataset.selection;
    get('download-report').replaceChildren();
    get('download-original').textContent = '';
    get('download-comparison').hidden = true;
    get('download-note').hidden = true;
    get('download-details').hidden = true;
    get('download-details').open = false;
    for (const id of ['original-fold', 'candidate-fold']) get(id).open = false;
    get('review-art').hidden = true;
    get('transfer-label').textContent = '';
    get('download-spectrum-details').open = false;
    get('add-download').hidden = true;
  }
  selectedDownload = job;
  get('download-heading').hidden = false;
  get('download-title').textContent = job.title;
  get('download-source').textContent = job.peer;
  get('download-progress').max = job.total;
  get('download-progress').hidden = false;
  get('download-progress').removeAttribute('value');
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
    if (quiet && !selected && JSON.stringify(items) === JSON.stringify(downloadJobs)) return;
    downloadJobs = items;
    const list = get('download-list');
    list.replaceChildren(...items.map(job => {
      const button = element('button', '', 'download-job');
      button.dataset.job = job.id;
      const title = element('span');
      const label = {applied: 'Files remain in staging', reviewed: 'Reviewed', downloaded: 'Reviewing audio', downloading: 'Downloading'}[job.state];
      title.append(element('strong', job.title), element('small', [label, job.total + ' tracks'].filter(Boolean).join(' · ')));
      button.append(title);
      button.append(element('span', 'Open →', 'download-open'));
      button.addEventListener('click', () => {
        downloadOriginal = null;
        selectDownload(job, true);
      });
      return button;
    }));
    if (!items.length) {
      list.append(element('p', 'Downloads will appear here after Spliff finds a suitable offer.', 'download-empty'));
    }
    const job = items.find(item => item.id === (selected || selectedDownload?.id)) || items[0];
    if (job && (selected || downloadOriginal || get('downloads-panel').classList.contains('show-download'))) {
      selectDownload(job, Boolean(selected || downloadOriginal));
    } else if (job) {
      selectedDownload = null;
    }
    else {
      selectedDownload = null;
      get('download-spectrum').hidden = true;
      downloadRequest++;
      get('download-heading').hidden = true;
      get('download-comparison').hidden = true;
      get('download-report').replaceChildren();
      get('download-note').hidden = true;
      get('download-details').hidden = true;
      get('download-status').textContent = 'No completed downloads yet.';
      showDownloadList();
    }
  } catch (error) {
    if (request === downloadListRequest && !quiet) {
      if (!get('downloads-panel').classList.contains('show-download')) {
        get('download-list').replaceChildren(element('p', error.message, 'download-empty'));
      } else get('download-status').textContent = error.message;
    }
  } finally {
    if (request === downloadListRequest && !get('downloads-panel').hidden) {
      downloadTimer = setTimeout(() => loadDownloads(null, true), 8000);
    }
  }
}

function showDownload(result, job) {
  const review = result.items;
  const spectrum = get('download-spectrum');
  const spectrumKey = job.id + ':' + (result.comparison?.original_track_id || '');
  const spectrumDetails = get('download-spectrum-details');
  spectrumDetails.hidden = Boolean(result.kept_both) || !review.some(item => item.audio);
  spectrumDetails.ontoggle = () => {
    if (!spectrumDetails.open || spectrumDetails.hidden || spectrum.dataset.selection === spectrumKey) return;
    spectrum.dataset.selection = spectrumKey;
    spectrumViewer(spectrum, review.map((item, index) => ({title: item.audio?.title || item.name, index}))
      .filter(choice => review[choice.index].audio), choice => ({candidate: job.id, index: choice.index,
        ...(result.comparison ? {original: result.comparison.original_track_id} : {})}));
  };
  if (spectrumDetails.open) spectrumDetails.ontoggle();
  const matched = review.filter(item => item.assessment === 'possible match').length;
  const count = result.complete + ' of ' + review.length + (review.length === 1 ? ' track downloaded' : ' tracks downloaded');
  const progress = get('download-progress');
  progress.value = result.complete;
  progress.max = Math.max(1, review.length);
  progress.hidden = false;
  const complete = review.length > 0 && result.complete === review.length;
  get('transfer-label').textContent = complete ? '● Complete' : 'Downloading';
  get('candidate-label').textContent = complete ? 'Download ready for review' : 'Downloading tracks';
  const originals = [...new Map([...review.map(item => item.original).filter(Boolean),
    ...result.originals_without_match].map(track => [track.id, track])).values()];
  originals.sort((a, b) => (parseInt(a.disc_number) || 1) - (parseInt(b.disc_number) || 1) ||
    (parseInt(a.track_number) || 0) - (parseInt(b.track_number) || 0));
  get('original-list').replaceChildren(...originals.map(track => element('li', track.title || track.path)));
  get('candidate-list').replaceChildren(...review.map(item => element('li',
    (item.audio?.title || item.name) + ' · ' + (item.assessment || item.status))));
  for (const [id, tracks] of [['original-format', originals], ['candidate-format', review.map(item => item.audio)]]) {
    get(id).textContent = [...new Set(tracks.map(track => track?.codec?.toUpperCase()).filter(Boolean))].join(' / ');
  }
  get('original-fold').hidden = !originals.length;
  const art = get('review-art');
  const query = new URLSearchParams({candidate: job.id});
  if (result.comparison) query.set('original', result.comparison.original_track_id);
  const releases = result.edition_evidence?.releases;
  if (releases?.length === 1) query.set('edition', releases[0].id);
  const src = '/api/review-art?' + query;
  if (art.getAttribute('src') !== src || art.dataset.job !== job.id) {
    art.dataset.job = job.id;
    art.hidden = true;
    art.onload = () => { art.hidden = false; };
    art.onerror = () => { art.hidden = true; };
    art.src = src;
  } else if (art.complete && art.naturalWidth) art.hidden = false;
  get('download-source').textContent = [review.find(item => item.audio?.artist)?.audio.artist,
    result.kind === 'album' ? 'Album' : 'Track'].filter(Boolean).join(' · ');
  get('download-status').textContent = count + (result.comparison && !result.comparison.album_mismatch ? ' · ' + matched + (matched === 1 ? ' possible match' : ' possible matches') : '');
  const candidateAlbum = review.find(item => item.audio?.album)?.audio.album;
  get('download-title').textContent = candidateAlbum || job.title;
  get('download-comparison').hidden = false;
  const kept = result.kept_both;
  get('download-original').textContent = kept ? ({replace: 'FLACs in use', keep: 'Both versions saved', import: 'Added to library'}[kept.mode]) :
    result.comparison?.album || downloadOriginal?.title || 'No album selected';
  get('choose-original').textContent = companion && result.comparison?.album ?
    'Change album' : 'Choose comparison album';
  get('choose-original').hidden = Boolean(kept) || !result.complete;
  const eligible = !kept && result.comparison && !result.comparison.album_mismatch &&
    result.complete === review.length &&
    review.some(item => ['possible match', 'check track order', 'check featured credits'].includes(item.assessment) &&
      item.audio?.quality === 'lossless' &&
      item.original?.quality === 'lossy' && !item.original.protected);
  get('review-choices').hidden = !eligible;
  get('add-download').hidden = Boolean(kept) || eligible || !review.length || result.complete !== review.length ||
    !review.every(item => item.audio?.quality === 'lossless');
  keptComparison = eligible ? {candidate: job.id, original: result.comparison.original_track_id} : null;
  const inline = get('inline-options');
  const planKey = eligible ? JSON.stringify(keptComparison) : '';
  if (inline.dataset.key !== planKey) {
    inline.dataset.key = planKey;
    inline.replaceChildren();
    if (eligible) showUpgradeOptions(keptComparison, false, inline);
  }
  const edition = result.edition_evidence;
  const editionNote = get('download-edition');
  editionNote.hidden = !edition;
  editionNote.replaceChildren();
  if (edition) {
    editionNote.append(edition.status);
    for (const release of edition.releases) {
      editionNote.append(` · ${release.title} (${release.year || 'year unknown'})`);
    }
  }
  const note = get('download-note');
  let message = '';
  if (result.completion) message = completionText(result.completion);
  else if (kept) message = kept.mode === 'replace' ? 'Lossless upgrades are in use.' : 'Both versions were saved.';
  else if (result.comparison?.album_mismatch) message = 'Choose the matching album in your library.';
  else if (review.some(item => item.original?.protected)) message = 'This album includes protected tracks.';
  else if (result.comparison && !eligible && result.kind === 'album') {
    message = companion ? 'Spliff couldn’t match every track. Open Track details to see what needs review.' :
      'Review the track details before using this download.';
  }
  note.textContent = message;
  note.hidden = !message;
  get('download-details').hidden = false;
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
    if (result.state === 'applied') {
      get('queue-status').textContent = 'This transfer was already installed.';
    } else {
      get('queue-status').textContent = 'Sent to Nicotine+. It will appear here when complete.';
      await loadDownloads(result.id, true);
    }
  } catch (error) {
    get('queue-status').textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

const keepDialog = get('keep-both-dialog');
const keepStatus = get('keep-both-status');
let applyingUpgrade = false;
let refreshUpgradePlans = false;
let upgradeOptionsRequest = 0;

function completionText(result) {
  const counts = result.mode === 'import' ? `Added ${result.imported} tracks to ${result.destination}` :
    `${result.upgraded} upgraded · ${result.preserved} originals preserved · ${result.skipped.length} skipped · ${result.failed} failed`;
  const staging = result.staging_cleared ? 'Staging cleared.' :
    result.staging_path ? `Review the files left in staging at ${result.staging_path}.` : '';
  const recovery = result.trash_pending ? `Originals awaiting Trash are recoverable at ${result.trash_pending}.` :
    result.recovery_path ? `Review the remaining workspace at ${result.recovery_path}.` : '';
  return [counts + '.', staging, recovery].filter(Boolean).join(' ');
}

async function showUpgradeOptions(comparison, refresh = false, inline = null) {
  if (!comparison || keepDialog.open) return;
  const request = ++upgradeOptionsRequest;
  const options = inline || get('upgrade-options');
  const status = inline ? get('inline-choice-status') : keepStatus;
  const current = () => request === upgradeOptionsRequest && (inline ? inline.dataset.key === JSON.stringify(comparison) : keepDialog.open);
  options.replaceChildren();
  status.textContent = 'Checking audio and destinations…';
  if (!inline) keepDialog.showModal();
  const query = new URLSearchParams({...comparison, refresh: refresh || refreshUpgradePlans ? '1' : '0'});
  try {
    const result = await action('/api/upgrade-options?' + query);
    if (!current()) return;
    refreshUpgradePlans = false;
    status.textContent = '';
    for (const [mode, label] of [['keep', 'Keep both'], ['replace', 'Replace']]) {
      const plan = result.options[mode];
      const card = element('section', '', 'upgrade-option');
      if (!plan) card.append(element('h3', label));
      if (!plan) {
        card.append(element('p', result.blocked[mode], 'muted'));
        options.append(card);
        continue;
      }
      card.append(element('p', `${plan.count} ${plan.count === 1 ? 'upgrade' : 'upgrades'} → ${plan.destination}`),
        element('p', `Originals → ${plan.archive}`));
      if (plan.preserved.length || plan.skipped.length) {
        card.append(element('p', `${plan.preserved.length} originals stay in place · ${plan.skipped.length} downloads skipped`, 'muted'));
      }
      const details = element('details');
      details.open = !inline;
      details.append(element('summary', 'Exact files and audio review'));
      const files = element('ul');
      for (const file of plan.files) {
        const evidence = file.audio_evidence;
        files.append(element('li', `${file.install} · Original: ${file.original} → ${file.archive}` +
          (evidence ? ` · ${evidence.status}` : '')));
      }
      for (const skipped of plan.skipped) files.append(element('li', `Skipped ${skipped.download}: ${skipped.reason}`));
      details.append(files, element('p', plan.audio_review, 'muted'));
      const button = element('button', label);
      button.addEventListener('click', event => {
        if (event.isTrusted) applyUpgrade(comparison, mode, plan, status, options);
      });
      card.prepend(button);
      card.append(details);
      options.append(card);

    }
  } catch (error) {
    if (current()) {
      status.textContent = error.message;
      if (inline) delete inline.dataset.key;
    }
  }
}

const reviewLink = new URLSearchParams(location.search);
if (reviewLink.has('review_candidate') && reviewLink.has('review_original')) {
  showUpgradeOptions({candidate: reviewLink.get('review_candidate'), original: reviewLink.get('review_original')}, true);
}

get('cancel-keep-both').addEventListener('click', () => keepDialog.close());
keepDialog.addEventListener('close', () => { upgradeOptionsRequest++; });
keepDialog.addEventListener('cancel', event => { if (applyingUpgrade) event.preventDefault(); });

async function applyUpgrade(comparison, mode, plan, status, options) {
  if (applyingUpgrade) return;
  applyingUpgrade = true;
  get('cancel-keep-both').disabled = true;
  options.querySelectorAll('button').forEach(button => { button.disabled = true; });
  status.textContent = 'Verifying and installing upgrades…';
  try {
    const params = new URLSearchParams({...comparison, mode, token: plan.token});
    const result = await action('/api/keep-both?' + params, '', {'X-Spliff-Approval': plan.approval});
    keepDialog.close();
    if (result.staging_cleared) {
      showDownloadList();
      await loadDownloads();
    } else {
      await loadDownloads(comparison.candidate);
    }
    notice(completionText(result));
  } catch (error) {
    refreshUpgradePlans = true;
    status.textContent = error.message + ' Review the updated plans before choosing again.';
    delete get('inline-options').dataset.key;
  } finally {
    applyingUpgrade = false;
    get('cancel-keep-both').disabled = false;
  }
}

get('reload-download').addEventListener('click', refreshDownload);
get('choose-original').addEventListener('click', () => {
  const job = downloadJobs.find(item => item.id === selectedDownload?.id);
  if (job?.kind === 'album') {
    get('album-query').value = get('download-title').textContent;
    showAlbumLibrary(true);
    return;
  }
  get('search').value = '';
  get('scope').value = 'all';
  view('tracks');
  get('search').focus();
});

get('filters').addEventListener('submit', event => { event.preventDefault(); loadTracks(); });
get('scope').addEventListener('change', () => loadTracks());
get('more').addEventListener('click', () => loadTracks(false));
get('browse').addEventListener('click', () => view('tracks'));
get('staged').addEventListener('click', () => view('reviews'));
get('downloads').addEventListener('click', () => view('downloads'));
get('albums').addEventListener('click', () => showAlbumLibrary());

const settingsDialog = get('settings-dialog');
const settingsForm = get('replacement-settings');
const replacementSelect = get('replace-destination');
const metadataSelect = get('file-metadata');
const saveSettings = settingsForm.querySelector('button');
const settingsStatus = get('settings-status');

const transferToggle = get('allow-transfers');
const transferReadiness = get('transfer-readiness');
const transferConnection = get('transfer-connection');
const transferResult = get('transfer-setting-result');
const installBridge = get('install-nicotine-bridge');
let transferBusy = false;
let transferRequest = 0;

async function refreshTransferSettings() {
  if (!settingsDialog.open || transferBusy) return;
  const request = ++transferRequest;
  try {
    const status = await api('/api/transfer-readiness');
    if (request !== transferRequest || !settingsDialog.open) return;
    transferToggle.checked = status.permission === 'on';
    transferToggle.disabled = status.permission !== 'on' &&
      ['bridge_unavailable', 'bridge_update_required'].includes(status.code);
    installBridge.hidden = !['bridge_unavailable', 'bridge_update_required'].includes(status.code);
    const bridgeState = status.bridge_connected ? 'Nicotine+ bridge connected' : 'Nicotine+ bridge unavailable';
    const soulseekState = status.soulseek_connected === null ? '' :
      (status.soulseek_connected ? 'Soulseek connected' : 'Soulseek offline');
    transferConnection.textContent = [bridgeState, soulseekState].filter(Boolean).join(' · ');
    transferConnection.hidden = false;
    transferReadiness.textContent = status.message;
  } catch (error) {
    if (request !== transferRequest) return;
    transferToggle.disabled = true;
    transferConnection.hidden = true;
    transferReadiness.textContent = 'Could not check download readiness. Spliff will try again.';
  }
}

async function changeTransferSettings(url) {
  ++transferRequest;
  transferBusy = true;
  transferToggle.disabled = installBridge.disabled = true;
  try {
    const result = await action(url);
    transferResult.textContent = result.message || (result.enabled ? 'Download permission saved.' : 'Downloads turned off.');
  } catch (error) {
    transferResult.textContent = error.message;
  } finally {
    transferBusy = installBridge.disabled = false;
    await refreshTransferSettings();
  }
}

transferToggle.addEventListener('change', () => changeTransferSettings(
  '/api/transfer-permission?action=' + (transferToggle.checked ? 'enable' : 'stop')));
installBridge.addEventListener('click', () => changeTransferSettings('/api/install-nicotine-bridge'));
setInterval(refreshTransferSettings, 3000);

get('open-settings').addEventListener('click', async () => {
  settingsDialog.showModal();
  refreshTransferSettings();
  get('advanced').open = false;
  replacementSelect.disabled = metadataSelect.disabled = saveSettings.disabled = true;
  settingsStatus.textContent = 'Loading settings…';
  try {
    const [replacement, metadata] = await Promise.all([
      api('/api/replacement-preference'), api('/api/file-metadata-preference')
    ]);
    replacementSelect.value = replacement.destination;
    metadataSelect.value = metadata.mode;
    replacementSelect.disabled = metadataSelect.disabled = saveSettings.disabled = false;
    settingsStatus.textContent = '';
  } catch (error) {
    settingsStatus.textContent = error.message;
  }
});
get('close-settings').addEventListener('click', () => settingsDialog.close());
settingsForm.addEventListener('submit', async event => {
  event.preventDefault();
  replacementSelect.disabled = metadataSelect.disabled = saveSettings.disabled = true;
  try {
    await Promise.all([
      action('/api/replacement-preference?' + new URLSearchParams({destination: replacementSelect.value})),
      action('/api/file-metadata-preference?' + new URLSearchParams({mode: metadataSelect.value}))
    ]);
    settingsStatus.textContent = 'Saved.';
  } catch (error) {
    settingsStatus.textContent = error.message;
  } finally {
    replacementSelect.disabled = metadataSelect.disabled = saveSettings.disabled = false;
  }
});
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

let pickerOpen = false;
let lastScan;
let dismissedScan = false;
let checkingScan = false;

function updatePickerButtons() {
  const disabled = pickerOpen || Boolean(lastScan?.current);
  get('add-library').disabled = get('sync-folder').disabled = disabled;
}

async function scanMusic() {
  if (filePreview) {
    location.assign(get('file-preview').querySelector('a').href);
    return;
  }
  if (companion) {
    window.webkit.messageHandlers.spliffMac.postMessage('chooseMusic');
    return;
  }
  pickerOpen = true;
  updatePickerButtons();
  try {
    const result = await action('/api/scan');
    if (result.started) {
      get('action-status').hidden = true;
      view('sync');
      await loadScanProgress();
    }
  } catch (error) {
    notice(error.message);
  } finally {
    pickerOpen = false;
    updatePickerButtons();
  }
}

async function loadScanProgress() {
  if (checkingScan) return;
  checkingScan = true;
  try {
    const {scan} = await api('/api/scan-progress');
    const isScanning = Boolean(scan?.current);
    const changed = JSON.stringify(scan) !== JSON.stringify(lastScan);
    if (scan?.id !== lastScan?.id) dismissedScan = false;
    lastScan = scan;
    if (!scan || !changed) return;
    if (document.body.dataset.view === 'sync') loadSync();
    if (isScanning) dismissedScan = false;
    const count = `${scan.completed} / ${scan.total} folders`;
    const folder = scan.current?.split('/').filter(Boolean).pop() || scan.current;
    get('scan-label').textContent = isScanning ? `Reading ${folder} · ${count}` :
      `Scan finished · ${count}${scan.failures.length ? ` · ${scan.failures.length} failed` : ''}`;
    get('scan-bar').max = scan.total;
    get('scan-bar').value = scan.completed;
    get('scan-dismiss').hidden = isScanning;
    get('scan-errors').hidden = !scan.failures.length;
    get('scan-errors').replaceChildren(...scan.failures.map(item =>
      element('li', `${item.folder} · ${item.error}`)));
    get('scan-footer').hidden = dismissedScan;
    updatePickerButtons();
  } catch {
    // Progress is available again when the local dashboard reconnects.
  } finally {
    checkingScan = false;
  }
}

get('scan-dismiss').addEventListener('click', () => {
  dismissedScan = true;
  get('scan-footer').hidden = true;
});

let syncTimer;
let syncRequest = 0;
async function loadSync() {
  if (filePreview) return;
  const request = ++syncRequest;
  clearTimeout(syncTimer);
  try {
    const {items, folders} = await api('/api/sync');
    if (request !== syncRequest) return;
    const counts = {};
    for (const item of items) counts[item.current_state] = (counts[item.current_state] || 0) + 1;
    const labels = {queued: 'Searching', candidate_ready: 'Awaiting selection', downloading: 'Downloading', queueing: 'Starting', reviewing: 'Reviewing audio', reviewed: 'Reviewed',
      applied: 'Applied', no_candidate: 'No offer', search_failed: 'Search failed', queue_uncertain: 'Transfer needs attention'};
    const status = Object.entries(counts).map(([key, count]) => `${labels[key]} · ${count}`).join('   ')
      || (folders.length && !lastScan?.current ? 'No eligible upgrades in the selected folders.' : '');
    const statusLine = get('sync-status');
    if (statusLine.textContent !== status) statusLine.textContent = status;
    statusLine.hidden = !status;
    const folderLabel = get('sync-folder-state');
    const folderCount = folders.length
      ? `${folders.length} ${folders.length === 1 ? 'folder' : 'folders'} selected`
      : 'No folders selected';
    if (folderLabel.textContent !== folderCount) folderLabel.textContent = folderCount;
    get('sync-folders').hidden = !folders.length;
    get('sync-folders').replaceChildren(...folders.map(folder => {
      const row = element('li');
      row.append(element('strong', folder.split('/').filter(Boolean).pop() || folder),
        element('small', folder, 'muted'));
      return row;
    }));
    get('sync-details').hidden = !items.length;
    get('sync-items').replaceChildren(...items.map(item => element('li', [item.query, labels[item.current_state], item.detail].filter(Boolean).join(' · '))));
  } catch (error) {
    get('sync-folder-state').textContent = 'Catalog unavailable';
    get('sync-status').textContent = error.message;
    get('sync-status').hidden = false;
  } finally {
    if (request === syncRequest && document.body.dataset.view === 'sync') syncTimer = setTimeout(loadSync, 5000);
  }
}
get('sync').addEventListener('click', () => view('sync'));
get('sync-downloads').addEventListener('click', () => view('downloads'));
get('sync-folder').addEventListener('click', scanMusic);
get('add-library').addEventListener('click', scanMusic);
const companion = Boolean(window.webkit?.messageHandlers?.spliffMac);
if (companion) {
  document.documentElement.classList.add('companion');
  get('browse').textContent = 'Find individual tracks';
  get('staged').textContent = 'Saved file comparisons';
  get('albums-heading').textContent = 'Upgrade an album';
  get('open-settings').hidden = true;
}
get('download-comparison-control').append(get('choose-original'));
view('sync');
if (filePreview) {
  document.documentElement.classList.add('file-preview');
  get('workspace-location').textContent = 'File preview';
  get('file-preview').hidden = false;
  get('sync-folder').textContent = 'Open local dashboard';
  get('sync-folder-state').textContent = 'Live dashboard required';
} else {
  loadScanProgress();
  setInterval(loadScanProgress, 1000);
}

// One graph, one track selector, one source switch. Analysis stays on this Mac.
function spectrumViewer(container, choices, parameters) {
  if (!choices.length) { container.hidden = true; return; }
  const picker = element('select');
  picker.setAttribute('aria-label', 'Track to analyze');
  choices.forEach((choice, index) => {
    const option = element('option', choice.title);
    option.value = index;
    picker.append(option);
  });
  const toolbar = element('div', '', 'spectrum-toolbar');
  const toggle = element('div', '', 'spectrum-switch');
  const buttons = {};
  let data = null;
  let selected = 'download';
  let serial = 0;
  const metadata = element('small');
  const status = element('p', 'Analyzing…');
  status.setAttribute('role', 'status');
  const canvas = document.createElement('canvas');
  canvas.setAttribute('role', 'img');
  const paint = async () => {
    const source = data?.[selected];
    if (!source) return;
    const request = serial;
    const encoded = atob(source.image_base64);
    const bytes = Uint8Array.from(encoded, char => char.charCodeAt(0));
    const image = await createImageBitmap(new Blob([bytes], {type: 'image/png'}));
    if (request !== serial || source !== data?.[selected]) { image.close(); return; }
    canvas.width = image.width;
    canvas.height = image.height;
    canvas.getContext('2d').drawImage(image, 0, 0);
    image.close();
    canvas.setAttribute('aria-label', source.name + ': frequency over time, color indicates level in dBFS');
    canvas.title = source.method + '. ' + source.conclusion;
    metadata.textContent = encoding(source.audio);
    metadata.title = source.method + '. ' + source.conclusion;
    for (const [key, button] of Object.entries(buttons)) {
      button.hidden = !data[key];
      button.setAttribute('aria-pressed', key === selected);
    }
    canvas.hidden = metadata.hidden = false;
  };
  for (const [key, label] of [['original', 'Original'], ['download', 'Download']]) {
    const button = element('button', label);
    button.type = 'button';
    button.hidden = true;
    button.addEventListener('click', () => { selected = key; paint().catch(error => { status.textContent = error.message; }); });
    buttons[key] = button;
    toggle.append(button);
  }
  toolbar.append(picker, toggle);
  container.replaceChildren(toolbar, metadata, status, canvas);
  container.hidden = false;
  const load = async () => {
    const request = ++serial;
    data = null;
    canvas.hidden = metadata.hidden = true;
    Object.values(buttons).forEach(button => { button.hidden = true; });
    status.textContent = 'Analyzing…';
    const query = new URLSearchParams(parameters(choices[Number(picker.value)]));
    try {
      const result = await api('/api/spectrum?' + query);
      if (request !== serial || !container.isConnected) return;
      data = result;
      if (!data[selected]) selected = data.download ? 'download' : 'original';
      status.textContent = '';
      await paint();
    } catch (error) {
      if (request === serial) status.textContent = error.message;
    }
  };
  picker.addEventListener('change', load);
  load();
}

function showTrackSpectrum(track) {
  const dialog = element('dialog', '', 'spectrum-dialog');
  const close = element('button', 'Close', 'spectrum-close');
  close.addEventListener('click', () => dialog.close());
  const graph = element('section', '', 'spectrum');
  dialog.append(close, graph);
  document.body.append(dialog);
  dialog.addEventListener('close', () => dialog.remove(), {once: true});
  dialog.showModal();
  spectrumViewer(graph, [{title: track.title || track.path.split('/').pop(), id: track.id}], choice => ({track: choice.id}));
}

const importDialog = get('import-dialog');
const importForm = get('import-details');
const importStatus = get('import-status');
const importPreview = get('import-preview');
let importCandidate;
let importEditions = [];
let importBusy = false;

function importField(name) { return importForm.elements.namedItem(name); }

function option(label, value) {
  const item = element('option', label);
  item.value = value;
  return item;
}

function updateCategory() {
  const posthumous = importField('category').value === 'Posthumous Albums';
  get('import-posthumous-row').hidden = !posthumous;
  importField('posthumous').required = posthumous;
  if (!posthumous) importField('posthumous').checked = false;
}

function selectEdition() {
  const edition = importEditions.find(item => String(item.id) === importField('edition').value);
  if (!edition) return;
  for (const key of ['artist', 'title', 'year', 'category']) {
    if (edition[key]) importField(key).value = edition[key];
  }
  updateCategory();
}

async function showImport(candidate) {
  if (!candidate || importDialog.open) return;
  importCandidate = candidate;
  importForm.reset();
  importForm.hidden = importPreview.hidden = true;
  importStatus.textContent = 'Checking the download and looking up editions…';
  importDialog.showModal();
  try {
    const result = await action('/api/import-options?' + new URLSearchParams({candidate}));
    if (!importDialog.open || importCandidate !== candidate) return;
    importField('root').replaceChildren(option('Choose a library', ''),
      ...result.roots.map(root => option(root, root)));
    if (result.roots.length === 1) importField('root').value = result.roots[0];
    importField('category').replaceChildren(option('Choose a category', ''),
      ...result.categories.map(category => option(category, category)));
    for (const key of ['artist', 'title', 'year', 'category']) importField(key).value = result.suggested[key];
    importEditions = result.editions;
    importField('edition').replaceChildren(option('Use my filing details', ''),
      ...importEditions.map(edition => option([
        edition.title, edition.year, edition.country,
        ...edition.labels.flatMap(label => [label.name, label.catno])
      ].filter(Boolean).join(' · '), String(edition.id))));
    get('import-edition-row').hidden = !importEditions.length;
    if (importEditions.length === 1) {
      importField('edition').value = String(importEditions[0].id);
      selectEdition();
    }
    updateCategory();
    importStatus.textContent = '';
    importForm.hidden = false;
    if (importEditions.length <= 1 && importForm.checkValidity()) {
      importForm.requestSubmit(importForm.querySelector('button[type="submit"]'));
    }
  } catch (error) {
    if (importDialog.open && importCandidate === candidate) importStatus.textContent = error.message;
  }
}

get('add-download').addEventListener('click', () => showImport(selectedDownload?.id));
get('import-close').addEventListener('click', () => importDialog.close());
importDialog.addEventListener('cancel', event => { if (importBusy) event.preventDefault(); });
importField('edition').addEventListener('change', selectEdition);
importField('category').addEventListener('change', updateCategory);
get('import-pick-root').addEventListener('click', async event => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const result = await action('/api/import-root');
    if (!result.root || !importDialog.open) return;
    const select = importField('root');
    if (![...select.options].some(item => item.value === result.root)) select.append(option(result.root, result.root));
    select.value = result.root;
    importStatus.textContent = '';
  } catch (error) {
    importStatus.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

importForm.addEventListener('submit', async event => {
  event.preventDefault();
  const submit = event.submitter;
  const candidate = importCandidate;
  submit.disabled = true;
  importStatus.textContent = 'Verifying files and checking destinations…';
  const data = Object.fromEntries(new FormData(importForm));
  data.candidate = candidate;
  data.edition = data.edition ? Number(data.edition) : null;
  data.posthumous = importField('posthumous').checked;
  try {
    const plan = await action('/api/import-plan', JSON.stringify(data), {'Content-Type': 'application/json'});
    if (!importDialog.open || importCandidate !== candidate) return;
    showImportPreview(plan);
  } catch (error) {
    if (importDialog.open && importCandidate === candidate) importStatus.textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

function showImportPreview(plan) {
  importForm.hidden = true;
  importPreview.hidden = false;
  importStatus.textContent = '';
  const folders = element('ul');
  for (const folder of plan.new_folders) folders.append(element('li', folder));
  const files = element('ul');
  for (const file of plan.files) {
    const item = element('li', file.install);
    if (Object.keys(file.tags).length) {
      const tags = element('ul');
      for (const [tag, value] of Object.entries(file.tags)) {
        const before = file.before[tag];
        tags.append(element('li', before && before !== value ? tag + ' · ' + before + ' → ' + value : tag + ' · ' + value));
      }
      item.append(tags);
    }
    files.append(item);
  }
  const edit = element('button', 'Edit filing details');
  edit.type = 'button';
  edit.addEventListener('click', () => { importPreview.hidden = true; importForm.hidden = false; });
  const approve = element('button', 'Add to library', 'primary');
  approve.type = 'button';
  approve.addEventListener('click', async event => {
    if (!event.isTrusted || importBusy) return;
    importBusy = approve.disabled = edit.disabled = get('import-close').disabled = true;
    importStatus.textContent = 'Verifying and filing your download…';
    try {
      const query = new URLSearchParams({candidate: plan.candidate, token: plan.token});
      const result = await action('/api/import-apply?' + query, '', {'X-Spliff-Approval': plan.approval});
      importDialog.close();
      showDownloadList();
      await loadDownloads(result.staging_cleared ? undefined : plan.candidate);
      notice(completionText(result));
    } catch (error) {
      importStatus.textContent = error.message;
    } finally {
      importBusy = edit.disabled = get('import-close').disabled = false;
    }
  });
  importPreview.replaceChildren(element('strong', plan.destination),
    ...(plan.edition ? [element('p', [plan.edition.artist, plan.edition.title, plan.edition.year,
      plan.edition.country, ...plan.edition.labels.flatMap(label => [label.name, label.catno])].filter(Boolean).join(' · '))] : []),
    element('h3', 'Folders to create'), folders,
    element('h3', plan.metadata_mode === 'write' ? 'Files and music tags' : 'Files preserved as downloaded'), files,
    edit, approve);
}

if (reviewLink.has('import_candidate')) showImport(reviewLink.get('import_candidate'));

if (!filePreview && new URLSearchParams(location.search).get('settings') === '1') get('open-settings').click();
