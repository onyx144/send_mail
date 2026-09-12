(function() {
  function extractEmailsFromText(text) {
    const emailRegex = /([a-zA-Z0-9._-]+@[a-zA-Z0-9._-]+\.[a-zA-Z0-9._-]+)/gi;
    const matches = text.match(emailRegex);
    return matches ? [...new Set(matches)] : [];
  }

  const bodyText = document.body.innerText || "";
  
  const links = Array.from(document.querySelectorAll('a[href^="mailto:"]')).map(a => a.href.replace('mailto:', '').split('?')[0]);
  
  const allEmails = [...extractEmailsFromText(bodyText), ...links];
  
  const uniqueEmails = [...new Set(allEmails)].filter(email => {
    return email.length < 254 && email.includes('@') && email.includes('.');
  });

  return uniqueEmails;
})();
