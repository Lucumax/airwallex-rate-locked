
(() => {
  const KEY='__rl_ptr';
  const place=(x,y)=>{const d=document.getElementById('__rl_cursor'); if(d){d.style.left=x+'px'; d.style.top=y+'px';} try{sessionStorage.setItem(KEY,x+','+y);}catch(e){}};
  const build=()=>{
    if(document.getElementById('__rl_cursor')||!document.documentElement) return;
    const st=document.createElement('style');
    st.textContent='#__rl_cursor{position:fixed;z-index:2147483647;pointer-events:none;width:0;height:0;left:-50px;top:-50px}'+
     '#__rl_cursor i{position:absolute;left:-15px;top:-15px;width:30px;height:30px;border-radius:50%;box-sizing:border-box;background:rgba(255,200,40,.35);border:3px solid #1d2733;box-shadow:0 0 0 2px #fff}'+
     '#__rl_cursor b{position:absolute;left:-5px;top:-5px;width:10px;height:10px;border-radius:50%;background:#e11d48}'+
     '#__rl_cursor u{position:absolute;left:-15px;top:-15px;width:30px;height:30px;border-radius:50%;border:4px solid #e11d48;opacity:0;box-sizing:border-box}'+
     '#__rl_cursor.down u{animation:rlr .55s ease-out}@keyframes rlr{0%{opacity:.95;transform:scale(1)}100%{opacity:0;transform:scale(3.2)}}'+
     '#__rl_hl{position:fixed;z-index:2147483646;pointer-events:none;border:5px solid #e11d48;border-radius:10px;box-shadow:0 0 0 4px rgba(255,255,255,.85),0 0 24px rgba(225,29,72,.45);transition:all .25s ease}'+
     '#__rl_label{position:fixed;z-index:2147483645;left:50%;top:64px;transform:translateX(-50%);background:#c2410c;color:#fff;font:700 24px Segoe UI,Arial,sans-serif;padding:10px 26px;border-radius:12px;box-shadow:0 4px 18px rgba(0,0,0,.35)}';
    document.documentElement.appendChild(st);
    const d=document.createElement('div'); d.id='__rl_cursor'; d.innerHTML='<u></u><i></i><b></b>'; document.documentElement.appendChild(d);
    try{const p=(sessionStorage.getItem(KEY)||'').split(','); if(p.length===2) place(+p[0],+p[1]);}catch(e){}
  };
  addEventListener('mousemove',e=>place(e.clientX,e.clientY),true);
  addEventListener('mousedown',()=>{const d=document.getElementById('__rl_cursor'); if(d){d.classList.remove('down'); void d.offsetWidth; d.classList.add('down');}},true);
  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',build); else build();
})();
