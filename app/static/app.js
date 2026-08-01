function openRegister(){document.getElementById('registerBackdrop')?.classList.add('open');document.getElementById('registerDrawer')?.classList.add('open');document.body.style.overflow='hidden'}
function closeRegister(){document.getElementById('registerBackdrop')?.classList.remove('open');document.getElementById('registerDrawer')?.classList.remove('open');document.body.style.overflow=''}
function filterPlatforms(){
  const generation=document.querySelector('input[name="generation"]:checked')?.value || 'nova';
  document.querySelectorAll('[data-generation]').forEach(el=>{
    const show=el.dataset.generation===generation;
    el.style.display=show?'block':'none';
    const input=el.querySelector('input');
    if(!show && input.checked) input.checked=false;
  });
  const visible=[...document.querySelectorAll('[data-generation]')].filter(el=>el.style.display!=='none');
  if(visible.length && !visible.some(el=>el.querySelector('input').checked)) visible[0].querySelector('input').checked=true;
}
document.addEventListener('DOMContentLoaded',()=>{
  document.querySelectorAll('input[name="generation"]').forEach(el=>el.addEventListener('change',filterPlatforms));
  filterPlatforms();
  if(document.body.dataset.openRegister==='1') openRegister();
});
