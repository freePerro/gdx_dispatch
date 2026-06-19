document.getElementById('f').addEventListener('submit', async function(e) {
  e.preventDefault();
  var msg = document.getElementById('msg');
  msg.textContent = 'Creating account...';
  var token = document.querySelector('script[src*="/form.js"]').src.split('/join/')[1].split('/form.js')[0];
  try {
    var res = await fetch('/api/supplier/register', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        token: token,
        company_name: document.getElementById('company').value,
        phone: document.getElementById('phone').value,
        password: document.getElementById('pw').value
      })
    });
    var data = await res.json();
    if (data.supplier_id) {
      msg.innerHTML = '<strong>Account created!</strong> You can now log in.';
    } else {
      msg.textContent = data.detail || 'Error creating account';
    }
  } catch (err) {
    msg.textContent = 'Network error — please try again.';
  }
});
