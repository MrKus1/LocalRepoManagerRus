
function toggleMenu() {
    document.getElementById('sideMenu').classList.toggle('open');
    document.getElementById('sideBackdrop').classList.toggle('show');
}
function fmtGb(n) { return (n / 1024 / 1024 / 1024).toFixed(1) + ' ГБ'; }
function loadSettings() {
    fetch('/api/proxy').then(r => r.json()).then(d => {
        const u = document.getElementById('proxyUrl');
        const n = document.getElementById('noProxy');
        if (u) u.value = d.proxy_url || '';
        if (n) n.value = d.no_proxy || '';
    });
    fetch('/api/min-free').then(r => r.json()).then(d => {
        const el = document.getElementById('minFree');
        if (el) el.value = d.min_free_gb || '';
    });
    const disk = document.getElementById('diskInfo');
    if (disk) {
        fetch('/api/disk').then(r => r.json()).then(d => {
            disk.textContent = 'Использование диска: ' + fmtGb(d.used_bytes) + ' / ' + fmtGb(d.total_bytes);
            const bar = document.getElementById('diskBar');
            if (bar && d.total_bytes) bar.style.width = Math.round(d.used_bytes / d.total_bytes * 100) + '%';
        }).catch(() => { disk.textContent = 'диск: нет данных'; });
    }
}
async function saveProxy() {
    const res = await fetch('/api/proxy', {
        method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({proxy_url: document.getElementById('proxyUrl').value, no_proxy: document.getElementById('noProxy').value})
    });
    if (!res.ok) { alert('Не удалось сохранить прокси'); return; }
    alert('Прокси сохранён');
}
async function saveMinFree() {
    const res = await fetch('/api/min-free', {
        method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({min_free_gb: document.getElementById('minFree').value})
    });
    if (!res.ok) { alert('Не удалось сохранить порог'); return; }
    alert('Порог сохранён');
}
document.addEventListener('DOMContentLoaded', loadSettings);
