function toast(msg, ok) {
    const el = document.getElementById('appToast');
    if (!el) { alert(msg); return; }
    el.textContent = msg;
    el.className = 'app-toast show' + (ok === false ? ' bad' : '');
    clearTimeout(el._t);
    el._t = setTimeout(() => el.classList.remove('show'), 3200);
}
let _deleteAction = null;
function askDelete(name, onChoice) {
    const text = document.getElementById('deleteText');
    const modal = document.getElementById('deleteModal');
    if (!text || !modal) { onChoice(confirm('Удалить файлы?')); return; }
    text.textContent = 'Удалить «' + name + '»?';
    _deleteAction = onChoice;
    bootstrap.Modal.getOrCreateInstance(modal).show();
}
function confirmDelete(withFiles) {
    bootstrap.Modal.getOrCreateInstance(document.getElementById('deleteModal')).hide();
    if (_deleteAction) _deleteAction(withFiles);
    _deleteAction = null;
}
let _okAction = null;
function askOk(text, onYes) {
    const el = document.getElementById('okText');
    const modal = document.getElementById('okModal');
    if (!el || !modal) { if (confirm(text)) onYes(); return; }
    el.textContent = text;
    _okAction = onYes;
    bootstrap.Modal.getOrCreateInstance(modal).show();
}
function confirmOk() {
    bootstrap.Modal.getOrCreateInstance(document.getElementById('okModal')).hide();
    if (_okAction) _okAction();
    _okAction = null;
}
function showError(el) {
    const text = el.textContent || '';
    if (!text) return;
    const box = document.getElementById('errorText');
    const modal = document.getElementById('errorModal');
    if (!box || !modal) { toast(text, false); return; }
    box.textContent = text;
    bootstrap.Modal.getOrCreateInstance(modal).show();
}
function statusRu(status) {
    if (status === 'success') return 'готово';
    if (status === 'running') return 'идёт';
    if (status === 'failed') return 'ошибка';
    return 'ожидает';
}
function closeMenu() {
    document.getElementById('sideMenu').classList.remove('open');
    document.getElementById('sideBackdrop').classList.remove('show');
}
function menuGo(name) {
    closeMenu();
    if (document.getElementById('tab-' + name)) { showTab(name); return false; }
    return true;
}
function updateRunCount() {
    const cards = document.querySelectorAll('.mirror-card');
    const running = document.querySelectorAll('.mirror-card[data-status="running"]').length;
    const failed = document.querySelectorAll('.mirror-card[data-status="failed"]').length;
    const enabled = document.querySelectorAll('.mirror-card[data-enabled="1"]').length;
    const visible = [...cards].filter(c => c.style.display !== 'none').length;
    const el = document.getElementById('runCount');
    if (el) el.textContent = running ? ('идёт ' + running) : '';
    const sum = document.getElementById('mirrorSummary');
    if (!sum) return;
    let text = 'включено ' + enabled + ' · в ошибке ' + failed + ' · качается ' + running;
    if (visible !== cards.length) text += ' · показано ' + visible;
    sum.textContent = text;
}
async function refreshLamp(el) {
    if (!el || !el.dataset.id) return;
    const res = await fetch('/api/mirrors/' + el.dataset.id + '/client-check');
    const data = await res.json().catch(() => ({ok: false}));
    el.className = 'reach-lamp ' + (data.ok ? 'lamp-green' : 'lamp-red');
    el.title = data.ok ? 'nginx отдал 200' : 'nginx не отдал репозиторий';
}
async function syncAll() {
    const res = await fetch('/api/mirrors/');
    const rows = await res.json().catch(() => []);
    let started = 0;
    let skipped = 0;
    for (const m of rows) {
        if (!m.enabled || m.status === 'running') { skipped += 1; continue; }
        const r = await fetch('/api/sync/' + m.id, {method: 'POST'});
        if (r.ok) {
            started += 1;
            const status = document.getElementById('status-' + m.id);
            if (status) {
                status.dataset.status = 'running';
                status.textContent = '● идёт';
                status.className = 'status-running';
                status.dataset.live = '1';
            }
            const card = document.getElementById('card-' + m.id);
            if (card) {
                card.className = 'card h-100 card-running';
                card.parentElement.dataset.status = 'running';
                const syncBtn = card.querySelector('.sync-btn');
                const stopBtn = card.querySelector('.stop-btn');
                const delBtn = card.querySelector('.delete-item');
                const wrap = document.getElementById('barwrap-' + m.id);
                const label = document.getElementById('progress-' + m.id);
                if (syncBtn) syncBtn.style.display = 'none';
                if (stopBtn) stopBtn.style.display = '';
                if (delBtn) delBtn.disabled = true;
                if (wrap) wrap.classList.remove('d-none');
                if (label) label.classList.remove('d-none');
            }
        } else skipped += 1;
    }
    updateRunCount();
    toast('Запущено ' + started + ', пропущено ' + skipped);
}
function cronHint(input, hintId) {
    const el = document.getElementById(hintId);
    if (!el) return;
    const text = humanCron(input.value);
    el.textContent = input.value.trim() ? text : 'пример: 0 3 * * * — каждый день в 03:00';
}
function copyMirrorLine(btn) {
    const host = location.hostname;
    const line = btn.dataset.type === 'rpm'
        ? '[' + btn.dataset.name + ']\nname=' + btn.dataset.name + '\nbaseurl=http://' + host + '/repo/rpm/' + btn.dataset.name + '\ngpgcheck=0\nenabled=1\n'
        : 'deb http://' + host + '/repo/deb/' + btn.dataset.name + ' ' + btn.dataset.dist + ' ' + btn.dataset.comp;
    navigator.clipboard.writeText(line).then(() => toast('Строка скопирована'));
}
function showTab(name) {
    ['mirrors', 'cache', 'local'].forEach(id => {
        const pane = document.getElementById('tab-' + id);
        if (pane) pane.classList.toggle('d-none', id !== name);
    });
    document.querySelectorAll('.page-tabs .nav-link').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.tab === name);
    });
    history.replaceState(null, '', '#' + name);
}
document.querySelectorAll('.page-tabs .nav-link').forEach(btn => {
    btn.addEventListener('click', () => showTab(btn.dataset.tab));
});
const startTab = (location.hash || '#mirrors').slice(1);
if (['mirrors', 'cache', 'local'].includes(startTab)) showTab(startTab);
function fmtGb(n) {
    return (n / 1024 / 1024 / 1024).toFixed(1) + ' ГБ';
}
function openSettings(id) {
    closeMenu();
    bootstrap.Modal.getOrCreateInstance(document.getElementById(id)).show();
}
function toggleMenu() {
    document.getElementById('sideMenu').classList.toggle('open');
    document.getElementById('sideBackdrop').classList.toggle('show');
}

fetch('/api/cache/cleanup').then(r => r.json()).then(d => {
    const c = document.getElementById('cacheCron');
    const n = document.getElementById('cacheDays');
    if (c) c.value = d.cron || '';
    if (n) n.value = d.days || '';
}).catch(() => {});
async function saveCacheCleanup() {
    const res = await fetch('/api/cache/cleanup', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            cron: document.getElementById('cacheCron').value,
            days: document.getElementById('cacheDays').value
        })
    });
    if (!res.ok) { toast((await res.json()).detail || res.status, false); return; }
    toast('Очистка сохранена');
}
async function runCacheCleanup() {
    const res = await fetch('/api/cache/cleanup/run', {method: 'POST'});
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { toast(data.detail || res.status, false); return; }
    toast('Удалено файлов: ' + (data.removed || 0));
}
fetch('/api/proxy').then(r => r.json()).then(d => {
    const u = document.getElementById('proxyUrl');
    const n = document.getElementById('noProxy');
    if (u) u.value = d.proxy_url || '';
    if (n) n.value = d.no_proxy || '';
});

fetch('/api/min-free').then(r => r.json()).then(d => {
    const minFree = document.getElementById('minFree');
    const maxSync = document.getElementById('maxSync');
    if (minFree) minFree.value = d.min_free_gb || '';
    if (maxSync) maxSync.value = d.max_sync || 2;
    const retryMinutes = document.getElementById('retryMinutes');
    const retryMax = document.getElementById('retryMax');
    if (retryMinutes) retryMinutes.value = d.retry_minutes || 60;
    if (retryMax) retryMax.value = d.retry_max ?? 3;
});

async function saveProxy() {
    const res = await fetch('/api/proxy', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            proxy_url: document.getElementById('proxyUrl').value,
            no_proxy: document.getElementById('noProxy').value
        })
    });
    if (!res.ok) { toast('Не удалось сохранить прокси', false); return; }
    toast('Прокси сохранён');
}

async function saveMinFree() {
    const res = await fetch('/api/min-free', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({min_free_gb: document.getElementById('minFree').value, max_sync: document.getElementById('maxSync').value, retry_minutes: document.getElementById('retryMinutes').value, retry_max: document.getElementById('retryMax').value})
    });
    if (!res.ok) { toast('Не удалось сохранить порог', false); return; }
    toast('Настройки места сохранены');
}

fetch('/api/disk').then(r => r.json()).then(d => {
    document.getElementById('diskInfo').textContent =
        fmtGb(d.used_bytes) + ' / ' + fmtGb(d.total_bytes);
    const bar = document.getElementById('diskBar');
    if (!bar || !d.total_bytes) return;
    const pct = d.used_bytes / d.total_bytes * 100;
    bar.style.width = Math.min(100, pct).toFixed(0) + '%';
    bar.className = pct < 70 ? 'disk-ok' : pct < 91 ? 'disk-warn' : 'disk-full';
}).catch(() => {
    document.getElementById('diskInfo').textContent = 'диск: нет данных';
});

function humanCron(expr) {
    const p = (expr || '').trim().split(/\s+/);
    if (p.length < 5) return expr;
    const [min, hour, dom, mon, dow] = p;
    const hh = hour.padStart(2, '0');
    const mm = min.padStart(2, '0');
    const days = {0:'вс',1:'пн',2:'вт',3:'ср',4:'чт',5:'пт',6:'сб',7:'вс'};
    if (min.startsWith('*/')) return 'каждые ' + min.slice(2) + ' мин';
    if (dom === '*' && mon === '*' && dow === '*') return 'каждый день в ' + hh + ':' + mm;
    if (dom === '*' && mon === '*' && dow !== '*') return 'по ' + (days[dow] || dow) + ' в ' + hh + ':' + mm;
    return expr;
}
document.querySelectorAll('.cron-human').forEach(el => {
    el.textContent = humanCron(el.dataset.cron);
});

async function uploadPkg(repoId, input) {
    const file = input.files[0];
    if (!file) return;
    const body = new FormData();
    body.append('file', file);
    const res = await fetch('/api/local-repos/' + repoId + '/packages', {method: 'POST', body});
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
        toast('Ошибка загрузки: ' + (data.detail || res.status), false);
        return;
    }
    toast('Пакет добавлен: ' + file.name);
    input.value = '';
}

async function importMirrors(input) {
    const file = input.files[0];
    if (!file) return;
    const body = new FormData();
    body.append('file', file);
    const res = await fetch('/api/mirrors/import', {method: 'POST', body});
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { toast(data.detail || res.status, false); return; }
    toast('Создано: ' + (data.created || []).join(', ') + (data.errors && data.errors.length ? '; ошибки: ' + data.errors.join(', ') : ''));
    location.reload();
}

function toggleCacheFields() {
    const type = document.getElementById('cacheType');
    const box = document.getElementById('cacheDebFields');
    if (type && box) box.classList.toggle('d-none', type.value !== 'deb');
}
async function createCache(e) {
    e.preventDefault();
    const data = Object.fromEntries(new FormData(e.target));
    const res = await fetch('/api/cache/', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(data)});
    if (!res.ok) { toast((await res.json()).detail || res.status, false); return false; }
    location.reload();
    return false;
}
fetch('/api/cache/').then(r => r.json()).then(rows => {
    const box = document.getElementById('cacheList');
    if (!box) return;
    const nl = String.fromCharCode(10);
    const count = document.getElementById('cacheCount');
    if (count) count.textContent = '(' + rows.length + ')';
    box.innerHTML = rows.map(r => {
        const line = r.type === 'rpm'
            ? '[' + r.name + ']' + nl + 'name=' + r.name + nl + 'baseurl=http://' + location.hostname + '/cache/' + r.name + '/' + nl + 'gpgcheck=0' + nl + 'enabled=1'
            : 'deb http://' + location.hostname + '/cache/' + r.name + ' ' + (r.distribution || 'bookworm') + ' ' + (r.component || 'main');
        const safe = line.replace(/</g, '');
        return '<div class="col-md-6 col-lg-4 mb-3"><div class="card h-100"><div class="card-body"><h5>' + r.name + '</h5><p class="small text-muted text-uppercase">' + r.type + '</p><p class="small text-truncate" title="' + r.source_url + '">источник: ' + r.source_url + '</p><div class="d-flex flex-wrap gap-1"><button class="btn btn-sm btn-outline-secondary" type="button" onclick="showCacheLine(' + r.id + ')">Строка</button><button class="btn btn-sm btn-outline-secondary" type="button" onclick="copyCache(' + r.id + ')">Копировать</button><div class="dropdown"><button class="btn btn-sm btn-outline-secondary dropdown-toggle" data-bs-toggle="dropdown" data-bs-boundary="viewport" type="button">Ещё</button><ul class="dropdown-menu"><li><button class="dropdown-item" type="button" onclick="downloadCache(' + r.id + ', \'' + r.name + '\', \'' + r.type + '\')">Скачать</button></li><li><button class="dropdown-item" type="button" onclick="editCache(' + r.id + ')">Ссылка</button></li><li><button class="dropdown-item" type="button" onclick="showCacheFiles(' + r.id + ', \'' + r.name + '\')">Файлы</button></li><li><button class="dropdown-item text-danger" type="button" onclick="deleteCache(' + r.id + ', \'' + r.name + '\')">Удалить</button></li></ul></div></div></div></div></div>';
    }).join('') || '<div class="col-12"><div class="alert alert-info">Кэшей нет. Пакет скачается при первом запросе клиента.</div></div>';
    rows.forEach(r => {
        const line = r.type === 'rpm'
            ? '[' + r.name + ']' + nl + 'name=' + r.name + nl + 'baseurl=http://' + location.hostname + '/cache/' + r.name + '/' + nl + 'gpgcheck=0' + nl + 'enabled=1'
            : 'deb http://' + location.hostname + '/cache/' + r.name + ' ' + (r.distribution || 'bookworm') + ' ' + (r.component || 'main');
        const node = document.getElementById('cline-' + r.id);
        if (node) node.textContent = line;
        window.cacheLines = window.cacheLines || {};
        window.cacheLines[r.id] = line;
        window.cacheUrls = window.cacheUrls || {};
        window.cacheUrls[r.id] = r.source_url;
    });
}).catch(() => {});
function showCacheLine(id) {
    document.getElementById('cacheLineBody').textContent = window.cacheLines[id] || '';
    bootstrap.Modal.getOrCreateInstance(document.getElementById('cacheLineModal')).show();
}
function copyCache(id) {
    navigator.clipboard.writeText(window.cacheLines[id] || '').then(() => toast('Строка скопирована'));
}
async function deleteCache(id, name) {
    askOk('Удалить кэш «' + name + '» и его файлы?', async () => {
        const res = await fetch('/api/cache/' + id, {method: 'DELETE'});
        if (!res.ok) { toast((await res.json().catch(() => ({}))).detail || res.status, false); return; }
        toast('Кэш удалён');
        location.reload();
    });
}
function downloadCache(id, name, type) {
    const text = window.cacheLines[id] || '';
    const blob = new Blob([text + '\n'], {type: 'text/plain'});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = name + (type === 'rpm' ? '.repo' : '.list');
    a.click();
}
async function editCache(id) {
    document.getElementById('cacheEditId').value = id;
    document.getElementById('cacheEditUrl').value = window.cacheUrls[id] || '';
    bootstrap.Modal.getOrCreateInstance(document.getElementById('cacheEditModal')).show();
}
async function saveCacheUrl(e) {
    e.preventDefault();
    const id = document.getElementById('cacheEditId').value;
    const url = document.getElementById('cacheEditUrl').value.trim();
    const res = await fetch('/api/cache/' + id, {method:'PUT', headers:{'Content-Type':'application/json'}, body: JSON.stringify({source_url: url})});
    if (!res.ok) { toast((await res.json()).detail || res.status, false); return false; }
    location.reload();
    return false;
}
async function showCacheFiles(id, name) {
    const res = await fetch('/api/cache/' + id + '/files');
    const data = await res.json();
    const rows = (data.files || []).map(f => {
        const mb = f.size / 1024 / 1024;
        const size = mb >= 1 ? mb.toFixed(1) + ' МБ' : Math.max(1, Math.round(f.size / 1024)) + ' КБ';
        return '<tr><td class="text-break">' + f.path + '</td><td class="text-end text-nowrap">' + size + '</td></tr>';
    }).join('');
    document.getElementById('cacheFilesTitle').textContent = name;
    document.getElementById('cacheFilesBody').innerHTML = rows || '<tr><td colspan="2">Пока пусто</td></tr>';
    bootstrap.Modal.getOrCreateInstance(document.getElementById('cacheFilesModal')).show();
}
async function cleanupAptly() {
    askOk('Удалить пакеты aptly, на которые не ссылается ни одно зеркало? Зеркала не трогает.', async () => {
        const res = await fetch('/api/mirrors/cleanup-aptly', {method: 'POST'});
        const data = await res.json().catch(() => ({}));
        if (!res.ok) { toast(data.detail || res.status, false); return; }
        toast('Готово');
    });
}
document.querySelectorAll('.reach-lamp').forEach(async el => {
    if (el.dataset.status === 'running') {
        el.className = 'reach-lamp lamp-yellow';
        el.title = 'синхронизируется';
        return;
    }
    const res = await fetch('/api/mirrors/' + el.dataset.id + '/client-check');
    const data = await res.json().catch(() => ({ok: false}));
    el.className = 'reach-lamp ' + (data.ok ? 'lamp-green' : 'lamp-red');
    el.title = data.ok ? 'nginx отдал 200' : 'nginx не отдал репозиторий';
    const label = document.getElementById('lamp-label-' + el.dataset.id);
    if (label) label.textContent = data.ok ? 'nginx : OK' : 'nginx : нет';
});

let currentFilter = 'all';
function applyMirrorView() {
    const q = (document.getElementById('mirrorSearch')?.value || '').toLowerCase();
    document.querySelectorAll('.mirror-card').forEach(card => {
        const nameOk = !q || card.dataset.name.includes(q);
        const filter = currentFilter;
        const filterOk = filter === 'all'
            || card.dataset.type === filter
            || (filter === 'failed' && card.dataset.status === 'failed')
            || (filter === 'running' && card.dataset.status === 'running');
        card.style.display = nameOk && filterOk ? '' : 'none';
    });
}
document.getElementById('mirrorSearch')?.addEventListener('input', applyMirrorView);
document.querySelectorAll('.filter-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.filter-btn').forEach(b => {
            b.classList.remove('active', 'btn-primary');
            b.classList.add('btn-outline-primary');
        });
        btn.classList.add('active', 'btn-primary');
        btn.classList.remove('btn-outline-primary');
        currentFilter = btn.dataset.filter;
        applyMirrorView();
    });
});

async function pollProgress() {
    const nodes = document.querySelectorAll('[id^="status-"]');
    for (const node of nodes) {
        const id = node.id.replace('status-', '');
        if (node.dataset.status !== 'running' && !node.dataset.live) continue;
        const res = await fetch('/api/sync/' + id + '/progress');
        if (!res.ok) continue;
        const data = await res.json();
        node.dataset.status = data.status;
        node.textContent = '● ' + statusRu(data.status);
        node.className = 'status-' + data.status;
        const card = document.getElementById('card-' + id);
        if (card) {
            card.className = 'card h-100 card-' + data.status;
            card.parentElement.dataset.status = data.status;
        }
        const syncBtn = card && card.querySelector('.sync-btn');
        const stopBtn = card && card.querySelector('.stop-btn');
        if (syncBtn) syncBtn.style.display = data.status === 'running' ? 'none' : '';
        if (stopBtn) stopBtn.style.display = data.status === 'running' ? '' : 'none';
        const delBtn = card && card.querySelector('.delete-item');
        if (delBtn) delBtn.disabled = data.status === 'running';
        const lamp = card && card.querySelector('.reach-lamp');
        if (lamp && data.status === 'running') {
            lamp.className = 'reach-lamp lamp-yellow';
            lamp.title = 'синхронизируется';
        } else if (lamp && data.status !== 'running') {
            refreshLamp(lamp);
        }
        const err = document.getElementById('err-' + id);
        if (err) {
            if (data.last_error) {
                err.textContent = data.last_error;
                err.classList.remove('d-none');
            } else {
                err.textContent = '';
                err.classList.add('d-none');
            }
        }
        const sizeEl = document.getElementById('size-' + id);
        if (sizeEl) sizeEl.textContent = data.size_bytes ? ('· ' + fmtGb(data.size_bytes)) : '';
        const synced = document.getElementById('synced-' + id);
        if (synced && data.last_sync) synced.textContent = '· ' + data.last_sync;
        node.dataset.live = data.status === 'running' ? '1' : '';
        const wrap = document.getElementById('barwrap-' + id);
        const bar = document.getElementById('bar-' + id);
        const label = document.getElementById('progress-' + id);
        if (data.status === 'running') {
            if (wrap) wrap.classList.remove('d-none');
            if (label) label.classList.remove('d-none');
            if (bar && data.percent !== null && data.percent !== undefined) {
                bar.style.width = data.percent + '%';
                bar.classList.remove('progress-bar-striped', 'progress-bar-animated');
                if (label) label.textContent = data.percent + '% · ' + data.files + ' пакетов';
            } else if (bar) {
                bar.style.width = '100%';
                bar.classList.add('progress-bar-striped', 'progress-bar-animated');
                if (label) label.textContent = 'идёт синхронизация · ' + data.files + ' пакетов';
            }
        } else {
            if (wrap) wrap.classList.add('d-none');
            if (label) { label.textContent = ''; label.classList.add('d-none'); }
        }
    }
    updateRunCount();
    if (typeof applyMirrorView === 'function') applyMirrorView();
    refreshRetries();
}
setInterval(pollProgress, 5000);
pollProgress();
updateRunCount();

let contentsMirrorId = null;
let contentsKind = 'mirror';
let contentsOffset = 0;
const pageSize = 50;

async function editMirror(id) {
    const res = await fetch('/api/mirrors/' + id);
    const m = await res.json();
    document.getElementById('editId').value = m.id;
    document.getElementById('editName').value = m.name;
    document.getElementById('editUrl').value = m.source_url || '';
    document.getElementById('editDistribution').value = m.distribution || '';
    document.getElementById('editComponents').value = m.components || '';
    document.getElementById('editArch').value = m.architectures || '';
    document.getElementById('editRepoid').value = m.repoid || '';
    document.getElementById('editSchedule').value = m.schedule || '';
    document.getElementById('editDescription').value = m.description || '';
    document.getElementById('editEnabled').checked = !!m.enabled;
    new bootstrap.Modal(document.getElementById('editMirrorModal')).show();
}

const editForm = document.getElementById('editMirrorForm');
if (editForm) editForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const id = document.getElementById('editId').value;
    const data = {
        source_url: document.getElementById('editUrl').value,
        distribution: document.getElementById('editDistribution').value,
        components: document.getElementById('editComponents').value,
        architectures: document.getElementById('editArch').value,
        repoid: document.getElementById('editRepoid').value,
        schedule: document.getElementById('editSchedule').value,
        description: document.getElementById('editDescription').value,
        enabled: document.getElementById('editEnabled').checked
    };
    const res = await fetch('/api/mirrors/' + id, {
        method: 'PATCH',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(data)
    });
    if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        toast('Ошибка: ' + (err.detail || res.status), false);
        return;
    }
    location.reload();
});

async function syncMirror(id) {
    const res = await fetch('/api/sync/' + id, {method: 'POST'});
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
        toast('Ошибка: ' + (data.detail || res.status), false);
        return;
    }
    const status = document.getElementById('status-' + id);
    if (status) {
        status.dataset.status = 'running';
        status.textContent = '● идёт';
        status.className = 'status-running';
        status.dataset.live = '1';
    }
    const card = document.getElementById('card-' + id);
    if (card) {
        card.className = 'card h-100 card-running';
        card.parentElement.dataset.status = 'running';
        const syncBtn = card.querySelector('.sync-btn');
        const stopBtn = card.querySelector('.stop-btn');
        const delBtn = card.querySelector('.delete-item');
        const wrap = document.getElementById('barwrap-' + id);
        const label = document.getElementById('progress-' + id);
        if (syncBtn) syncBtn.style.display = 'none';
        if (stopBtn) stopBtn.style.display = '';
        if (delBtn) delBtn.disabled = true;
        if (wrap) wrap.classList.remove('d-none');
        if (label) label.classList.remove('d-none');
    }
    updateRunCount();
}

async function stopMirror(id) {
    const res = await fetch('/api/sync/' + id + '/stop', {method: 'POST'});
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
        toast('Ошибка: ' + (data.detail || res.status), false);
        return;
    }
    const status = document.getElementById('status-' + id);
    if (status) {
        status.dataset.status = 'failed';
        status.textContent = '● ошибка';
        status.className = 'status-failed';
        status.dataset.live = '';
    }
    const card = document.getElementById('card-' + id);
    if (card) {
        card.className = 'card h-100 card-failed';
        card.parentElement.dataset.status = 'failed';
        const syncBtn = card.querySelector('.sync-btn');
        const stopBtn = card.querySelector('.stop-btn');
        const delBtn = card.querySelector('.delete-item');
        const wrap = document.getElementById('barwrap-' + id);
        const label = document.getElementById('progress-' + id);
        if (syncBtn) syncBtn.style.display = '';
        if (stopBtn) stopBtn.style.display = 'none';
        if (delBtn) delBtn.disabled = false;
        if (wrap) wrap.classList.add('d-none');
        if (label) label.classList.add('d-none');
    }
    updateRunCount();
}

async function deleteMirror(id, name) {
    askDelete(name, async (withFiles) => {
        const res = await fetch('/api/mirrors/' + id + '?delete_files=' + (withFiles ? 'true' : 'false'), {method: 'DELETE'});
        if (res.status === 204) {
            toast(withFiles ? 'Зеркало и файлы удалены' : 'Запись удалена');
            location.reload();
            return;
        }
        const data = await res.json().catch(() => ({}));
        toast('Ошибка: ' + (data.detail || res.status), false);
    });
}

async function deletePkg(filename) {
    if (contentsKind !== 'local') return;
    askOk('Удалить пакет ' + filename + '?', async () => {
        const res = await fetch('/api/local-repos/' + contentsMirrorId + '/packages/' + encodeURIComponent(filename), {method: 'DELETE'});
        if (res.status !== 204) {
            const data = await res.json().catch(() => ({}));
            toast(data.detail || res.status, false);
            return;
        }
        toast('Пакет удалён');
        loadContents();
    });
}

async function deleteLocal(id, name) {
    askDelete(name, async (withFiles) => {
        const res = await fetch('/api/local-repos/' + id + '?delete_files=' + (withFiles ? 'true' : 'false'), {method: 'DELETE'});
        if (res.status === 204) {
            toast(withFiles ? 'Репозиторий и файлы удалены' : 'Запись удалена');
            location.reload();
            return;
        }
        const data = await res.json().catch(() => ({}));
        toast('Ошибка: ' + (data.detail || res.status), false);
    });
}

async function loadContents() {
    const q = document.getElementById('contentsQuery').value || '';
    const base = contentsKind === 'local'
        ? '/api/local-repos/' + contentsMirrorId + '/contents'
        : '/api/mirrors/' + contentsMirrorId + '/contents';
    const res = await fetch(base + '?limit=' + pageSize + '&offset=' + contentsOffset + '&q=' + encodeURIComponent(q));
    const data = await res.json();
    const mb = (data.size_bytes || 0) / 1024 / 1024;
    const total = data.total || 0;
    const from = total ? data.offset + 1 : 0;
    const to = Math.min(data.offset + (data.packages || []).length, total);
    document.getElementById('contentsMeta').textContent =
        data.error ? data.error : ('Всего ' + total + ', размер ' + mb.toFixed(1) + ' МБ');
    document.getElementById('pageInfo').textContent = from + '–' + to + ' из ' + total;
    document.getElementById('prevPage').disabled = contentsOffset <= 0;
    document.getElementById('nextPage').disabled = contentsOffset + pageSize >= total;
    const list = document.getElementById('contentsList');
    list.innerHTML = (data.packages || []).map(p => {
        const del = contentsKind === 'local'
            ? ' <button class="btn btn-sm btn-outline-danger py-0" onclick="deletePkg(\'' + p.name.replace(/'/g, '') + '\')">удалить</button>'
            : '';
        return '<div class="border-bottom py-1 d-flex justify-content-between"><span><code>' + p.name + '</code> <span class="text-muted small">' +
            (p.size_bytes / 1024 / 1024).toFixed(2) + ' МБ</span></span>' + del + '</div>';
    }).join('') || '<div class="text-muted">' + (data.error || 'Пакетов не найдено') + '</div>';
}

function showContents(id, name, kind) {
    contentsMirrorId = id;
    contentsKind = kind || 'mirror';
    contentsOffset = 0;
    document.getElementById('contentsTitle').textContent = 'Содержимое: ' + name;
    document.getElementById('contentsQuery').value = '';
    const modal = new bootstrap.Modal(document.getElementById('contentsModal'));
    modal.show();
    loadContents();
}

const cq = document.getElementById('contentsQuery');
if (cq) cq.addEventListener('input', () => {
    clearTimeout(window._cq);
    window._cq = setTimeout(() => { contentsOffset = 0; loadContents(); }, 300);
});
const prev = document.getElementById('prevPage');
if (prev) prev.addEventListener('click', () => {
    contentsOffset = Math.max(0, contentsOffset - pageSize);
    loadContents();
});
const next = document.getElementById('nextPage');
if (next) next.addEventListener('click', () => {
    contentsOffset += pageSize;
    loadContents();
});

const mt = document.getElementById('mirrorType');
if (mt) mt.addEventListener('change', function() {
    const isDeb = this.value === 'deb';
    document.querySelectorAll('.deb-only').forEach(el => el.style.display = isDeb ? 'block' : 'none');
    document.querySelectorAll('.rpm-only').forEach(el => el.style.display = isDeb ? 'none' : 'block');
});

const amf = document.getElementById('addMirrorForm');
if (amf) amf.addEventListener('submit', async (e) => {
    e.preventDefault();
    const form = e.target;
    const data = Object.fromEntries(new FormData(form));
    data.enabled = true;
    
    const res = await fetch('/api/mirrors/', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(data)
    });
    
    if (res.ok) {
        location.reload();
    } else {
        const err = await res.json();
        toast('Ошибка: ' + (err.detail || JSON.stringify(err)), false);
    }
});

const alrf = document.getElementById('addLocalRepoForm');
if (alrf) alrf.addEventListener('submit', async (e) => {
    e.preventDefault();
    const form = e.target;
    const data = Object.fromEntries(new FormData(form));
    
    const res = await fetch('/api/local-repos/', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(data)
    });
    
    if (res.ok) {
        location.reload();
    } else {
        const err = await res.json();
        toast('Ошибка: ' + (err.detail || JSON.stringify(err)), false);
    }
});

function loadUpdateStatus() {
    fetch('/api/update').then(r => r.json()).then(d => {
        const el = document.getElementById('updateCurrent');
        if (el) el.textContent = d.current || '?';
        const info = document.getElementById('updateInfo');
        if (info) info.textContent = d.has_backup ? 'Есть резервная копия для отката.' : 'Резервной копии ещё нет.';
    }).catch(() => {});
    fetch('/api/update/log').then(r => r.json()).then(d => {
        const log = document.getElementById('updateLog');
        if (log) log.textContent = (d.lines || []).join('\n') || 'Журнал обновлений пуст.';
    }).catch(() => {});
}
document.getElementById('updateModal')?.addEventListener('show.bs.modal', loadUpdateStatus);

async function applyUpdate() {
    const input = document.getElementById('updateFile');
    const info = document.getElementById('updateInfo');
    if (!input.files.length) { toast('Выберите архив', false); return; }
    const body = new FormData();
    body.append('file', input.files[0]);
    info.textContent = 'Загрузка архива…';
    let res = await fetch('/api/update/upload', {method: 'POST', body});
    let data = await res.json().catch(() => ({}));
    if (!res.ok) { info.textContent = data.detail || 'Архив не принят'; toast(info.textContent, false); return; }
    info.textContent = 'Архив ' + data.archive + '. Запуск…';
    res = await fetch('/api/update/apply', {method: 'POST'});
    data = await res.json().catch(() => ({}));
    if (!res.ok) { info.textContent = data.detail || 'Не запустилось'; toast(info.textContent, false); return; }
    info.textContent = 'Обновление идёт, страница перезагрузится.';
    toast('Обновление запущено');
    waitForRestart();
}

async function rollbackUpdate() {
    if (!confirm('Вернуть предыдущую версию кода?')) return;
    const res = await fetch('/api/update/rollback', {method: 'POST'});
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { toast(data.detail || 'Откат не запустился', false); return; }
    toast('Откат запущен');
    waitForRestart();
}

function waitForRestart() {
    let tries = 0;
    const timer = setInterval(async () => {
        tries += 1;
        try {
            const res = await fetch('/health', {cache: 'no-store'});
            if (res.ok && tries > 2) {
                clearInterval(timer);
                location.reload();
            }
        } catch (e) {}
        if (tries > 40) clearInterval(timer);
    }, 2000);
}


async function refreshRetries() {
    try {
        const res = await fetch('/api/sync/retries');
        if (!res.ok) return;
        const data = await res.json();
        document.querySelectorAll('[id^="retry-"]').forEach(el => {
            const id = el.id.replace('retry-', '');
            const card = document.getElementById('card-' + id);
            const failed = card && card.parentElement && card.parentElement.dataset.status === 'failed';
            const item = data[id] || {};
            el.textContent = failed && item.count ? (' · повтор ' + item.count + '/' + item.max) : '';
        });
    } catch (e) {}
}
setInterval(refreshRetries, 10000);
refreshRetries();
