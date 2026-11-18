'use strict';
for (const button of document.querySelectorAll('[data-copy]')) {
  button.hidden = false;
  button.addEventListener('click', async () => {
    const status = document.querySelector('#copy-status');
    try {
      await navigator.clipboard.writeText(document.getElementById(button.dataset.copy).textContent);
      status.textContent = 'Copied to clipboard.';
      button.textContent = 'Copied';
      setTimeout(() => { button.textContent = button.dataset.copy === 'fingerprint' ? 'Copy fingerprint' : 'Copy'; }, 1800);
    } catch {
      status.textContent = 'Copy unavailable. Select and copy the text manually.';
    }
  });
}
const controls = document.querySelector('#catalog-controls');
if (controls) {
  controls.hidden = false;
  controls.addEventListener('submit', event => event.preventDefault());
  const rows = [...document.querySelectorAll('#packages tr')];
  const search = document.querySelector('#search');
  const debug = document.querySelector('#show-debug');
  const sort = document.querySelector('#sort');
  const update = () => {
    const query = search.value.trim().toLocaleLowerCase();
    rows.sort((a, b) => {
      const difference = sort.value === 'name' ? 0 : (Number(a.dataset.size) - Number(b.dataset.size)) * (sort.value === 'size-desc' ? -1 : 1);
      return difference || a.dataset.name.localeCompare(b.dataset.name);
    });
    let visible = 0;
    for (const row of rows) {
      row.hidden = (!debug.checked && row.dataset.debug === 'true') || !row.dataset.search.toLocaleLowerCase().includes(query);
      if (!row.hidden) visible++;
      document.querySelector('#packages').append(row);
    }
    document.querySelector('#package-count').textContent = `${visible} of ${rows.length} packages shown`;
    document.querySelector('#no-results').hidden = visible !== 0 || rows.length === 0;
  };
  controls.addEventListener('input', update);
  controls.addEventListener('change', update);
  update();
}
