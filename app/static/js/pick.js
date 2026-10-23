function pickedIds() {
    return [...document.querySelectorAll('.pick-mirror:checked')].map(el => ({
        id: el.dataset.id,
        busy: ['running', 'queued'].includes(el.closest('.mirror-card')?.dataset.status)
    }));
}
function visiblePickBoxes() {
    return [...document.querySelectorAll('.mirror-card')]
        .filter(card => card.style.display !== 'none')
        .map(card => card.querySelector('.pick-mirror'))
        .filter(Boolean);
}
function updatePickBar() {
    const n = document.querySelectorAll('.pick-mirror:checked').length;
    const count = document.getElementById('pickCount');
    if (count) count.textContent = 'выбрано ' + n;
    const sync = document.getElementById('syncPicked');
    const del = document.getElementById('deletePicked');
    if (sync) sync.disabled = n === 0;
    if (del) del.disabled = n === 0;
    const all = document.getElementById('pickAll');
    if (all) {
        const boxes = visiblePickBoxes();
        const checked = boxes.filter(b => b.checked).length;
        all.checked = boxes.length > 0 && checked === boxes.length;
        all.indeterminate = checked > 0 && checked < boxes.length;
    }
}
function togglePickAll(on) {
    visiblePickBoxes().forEach(box => { box.checked = on; });
    updatePickBar();
}
function markRunning(id) {
    const status = document.getElementById('status-' + id);
    if (status) {
        status.dataset.status = 'running';
        status.textContent = '● идёт';
        status.className = 'status-running';
        status.dataset.live = '1';
    }
    const card = document.getElementById('card-' + id);
    if (!card) return;
    card.className = 'card h-100 card-running';
    if (card.parentElement) card.parentElement.dataset.status = 'running';
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
async function syncPicked() {
    const rows = pickedIds();
    if (!rows.length) return;
    let started = 0;
    let queued = 0;
    let skipped = 0;
    for (const row of rows) {
        if (row.busy) { skipped += 1; continue; }
        const r = await fetch('/api/sync/' + row.id, {method: 'POST'});
        const data = await r.json().catch(() => ({}));
        if (!r.ok) { skipped += 1; continue; }
        if (data.queued) {
            queued += 1;
            if (typeof markQueued === 'function') markQueued(row.id);
            else markRunning(row.id);
        } else {
            started += 1;
            markRunning(row.id);
        }
    }
    if (typeof updateRunCount === 'function') updateRunCount();
    toast('Запущено ' + started + ', в очереди ' + queued + ', пропущено ' + skipped);
}
async function deletePicked() {
    const rows = pickedIds();
    if (!rows.length) { toast('Сначала отметьте зеркала', false); return; }
    askDelete(rows.length + ' зеркал', async (withFiles) => {
        const res = await fetch('/api/mirrors/bulk-delete', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ids: rows.map(row => Number(row.id)), delete_files: !!withFiles})
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) { toast(data.detail || res.status, false); return; }
        const started = data.started || [];
        const deleted = data.deleted || [];
        const errors = data.errors || [];
        if (started.length) {
            toast('Удаление запущено: ' + started.length + (deleted.length ? ', уже убрано ' + deleted.length : ''));
            if (typeof pollJobs === 'function') pollJobs();
            if (errors.length) toast(errors[0], false);
            return;
        }
        toast('Удалено ' + deleted.length + (errors.length ? '. ' + errors[0] : ''), errors.length === 0);
        if (deleted.length) location.reload();
    });
}
document.getElementById('mirrorSearch')?.addEventListener('input', () => setTimeout(updatePickBar, 0));
document.querySelectorAll('.filter-btn').forEach(btn => {
    btn.addEventListener('click', () => setTimeout(updatePickBar, 0));
});
