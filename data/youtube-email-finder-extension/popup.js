document.addEventListener('DOMContentLoaded', () => {
  const extractBtn = document.getElementById('extractBtn');
  const resultsContainer = document.getElementById('resultsContainer');
  const toast = document.getElementById('toast');

  function showToast(message) {
    toast.textContent = message;
    toast.classList.add('show');
    setTimeout(() => {
      toast.classList.remove('show');
    }, 2000);
  }

  function displayEmails(emails) {
    resultsContainer.innerHTML = '';
    resultsContainer.classList.add('active');

    if (!emails || emails.length === 0) {
      resultsContainer.innerHTML = '<div class="no-results">No emails found on this page.</div>';
      return;
    }

    emails.forEach(email => {
      const div = document.createElement('div');
      div.className = 'email-item';
      
      const emailText = document.createElement('span');
      emailText.textContent = email;
      
      const copyBtn = document.createElement('button');
      copyBtn.className = 'copy-btn';
      copyBtn.textContent = 'Copy';
      copyBtn.onclick = () => {
        navigator.clipboard.writeText(email).then(() => {
          showToast('Copied to clipboard!');
        });
      };

      div.appendChild(emailText);
      div.appendChild(copyBtn);
      resultsContainer.appendChild(div);
    });
  }

  extractBtn.addEventListener('click', async () => {
    extractBtn.textContent = 'Extracting...';
    extractBtn.disabled = true;

    try {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      
      if (!tab.url.includes("youtube.com")) {
        displayEmails([]);
        extractBtn.textContent = 'Extract Emails';
        extractBtn.disabled = false;
        showToast('Please open a YouTube page.');
        return;
      }

      chrome.scripting.executeScript({
        target: { tabId: tab.id },
        files: ['content.js']
      }, (injectionResults) => {
        extractBtn.textContent = 'Extract Emails';
        extractBtn.disabled = false;
        
        if (chrome.runtime.lastError || !injectionResults || !injectionResults.length) {
          console.error("Injection failed: ", chrome.runtime.lastError);
          displayEmails([]);
          return;
        }

        const result = injectionResults[0].result;
        displayEmails(result);
      });
    } catch (err) {
      console.error(err);
      extractBtn.textContent = 'Extract Emails';
      extractBtn.disabled = false;
      displayEmails([]);
    }
  });
});
