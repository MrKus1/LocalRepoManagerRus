(function () {
    const root = document.getElementById('mirrorPage');
    if (!root) return;
    const id = root.dataset.id;
    const name = root.dataset.name;

    function setButtons(status) {
        const syncBtn = document.getElementById('syncBtn');
        const stopBtn = document.getElementById('stopBtn');
        if (syncBtn) syncBtn.style.display = (status === 'running' || status === 'queued') ? 'none' : '';
        if (stopBtn) stopBtn.style.display = (status === 'running' || status === 'queued') ? '' : 'none';
        const del = document.getElementById('deleteBtn');
        if (del) del.disabled = status === 'running' || status === 'queued';
        const statusEl = document.getElementById('mirrorStatus');
        if (statusEl) {
            statusEl.textContent = typeof statusRu === 'function' ? statusRu(status) : status;
            statusEl.className = 'status-' + status;
        }
    }

    fetch('/api/mirrors/' + id + '/client-check').then(r => r.json()).then(d => {
        const lamp = document.getElementById('lamp');
        if (!lamp) return;
        if (root.dataset.status === 'running') {
            lamp.className = 'reach-lamp lamp-yellow';
            lamp.title = 'синхронизируется';
            return;
        }
        lamp.className = 'reach-lamp ' + (d.ok ? 'lamp-green' : 'lamp-red');
        lamp.title = d.ok ? 'nginx отдаёт' : 'nginx не отдаёт ' + (d.http_code || '');
    });

    setInterval(async () => {
        const res = await fetch('/api/sync/' + id + '/progress');
        if (!res.ok) return;
        const data = await res.json();
        setButtons(data.status);
        const lamp = document.getElementById('lamp');
        if (lamp && data.status === 'running') {
            lamp.className = 'reach-lamp lamp-yellow';
            lamp.title = 'синхронизируется';
        }
    }, 5000);

    setInterval(async () => {
        const res = await fetch('/api/sync/' + id + '/logs');
        if (!res.ok) return;
        const logs = await res.json();
        const body = document.querySelector('#history tbody');
        const box = document.querySelector('#history') && document.querySelector('#history').parentElement;
        if (!body) return;
        const next = logs.map(l => {
            const t = l.started_at ? String(l.started_at).replace('T', ' ').slice(8, 16).replace(/^(\d{2})-(\d{2})/, '$2.$1') : '';
            const when = l.started_at ? (function () {
                const d = new Date(l.started_at);
                if (Number.isNaN(d.getTime())) return t;
                const p = n => String(n).padStart(2, '0');
                return p(d.getDate()) + '.' + p(d.getMonth() + 1) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
            })() : '';
            return '<tr><td class="text-nowrap">' + when + '</td><td>' + (l.status === 'success' ? 'прошла' : l.status === 'running' ? 'идёт' : 'не прошла') + '</td></tr>';
        }).join('') || '<tr><td colspan="2">Запусков ещё нет.</td></tr>';
        if (body.innerHTML === next) return;
        const top = box ? box.scrollTop : 0;
        body.innerHTML = next;
        if (box) box.scrollTop = top;
    }, 5000);

    let offset = 0;
    const pageSize = 50;
    async function load() {
        const q = document.getElementById('q').value || '';
        const res = await fetch('/api/mirrors/' + id + '/contents?limit=' + pageSize + '&offset=' + offset + '&q=' + encodeURIComponent(q));
        const data = await res.json();
        const total = data.total || 0;
        document.getElementById('meta').textContent = 'Всего ' + total;
        const from = total ? offset + 1 : 0;
        const to = Math.min(offset + (data.packages || []).length, total);
        document.getElementById('pkgPage').textContent = from + '–' + to + ' из ' + total;
        document.getElementById('pkgPrev').disabled = offset <= 0;
        document.getElementById('pkgNext').disabled = offset + pageSize >= total;
        document.getElementById('list').innerHTML = (data.packages || []).map(p => '<div class="border-bottom py-1"><code>' + p.name + '</code></div>').join('') || 'пусто';
    }
    const q = document.getElementById('q');
    if (q) q.addEventListener('input', () => { offset = 0; load(); });
    document.getElementById('pkgPrev').addEventListener('click', () => { offset = Math.max(0, offset - pageSize); load(); });
    document.getElementById('pkgNext').addEventListener('click', () => { offset += pageSize; load(); });
    load();

    const form = document.getElementById('editForm');
    if (form) form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const body = {};
        ['source_url','distribution','components','architectures','repoid','schedule','description'].forEach(k => body[k] = document.getElementById(k).value);
        const res = await fetch('/api/mirrors/' + id, {method:'PATCH', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
        if (!res.ok) { toast('Ошибка сохранения', false); return; }
        toast('Сохранено');
    });

    window.mirrorSync = async function () {
        const res = await fetch('/api/sync/' + id, {method:'POST'});
        const data = await res.json().catch(() => ({}));
        if (!res.ok) { toast(data.detail || res.status, false); return; }
        setButtons(data.queued ? 'queued' : 'running');
    };
    window.mirrorStop = async function () {
        const res = await fetch('/api/sync/' + id + '/stop', {method:'POST'});
        if (!res.ok) { const d = await res.json().catch(() => ({})); toast(d.detail || res.status, false); return; }
        setButtons('failed');
    };
    window.mirrorDelete = function () {
        askDelete(name, async (withFiles) => {
            const res = await fetch('/api/mirrors/' + id + '?delete_files=' + (withFiles ? 'true' : 'false'), {method: 'DELETE'});
            if (res.status === 204) {
                toast(withFiles ? 'Зеркало и файлы удалены' : 'Запись удалена');
                location.href = '/';
                return;
            }
            const data = await res.json().catch(() => ({}));
            toast(data.detail || res.status, false);
        });
    };
    window.pruneOld = async function () {
        askOk('Удалить старые версии RPM? Новые останутся.', async () => {
            const res = await fetch('/api/mirrors/' + id + '/prune-old', {method:'POST'});
            const data = await res.json().catch(() => ({}));
            if (!res.ok) { toast(data.detail || res.status, false); return; }
            toast('Удалено: ' + data.removed);
            load();
        });
    };
})();
