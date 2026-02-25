const express = require('express');
const path = require('path');
const fs = require('fs');

const app = express();
const PORT = 8080;

const VIEWER_DIR = __dirname;
const PROCESSED_DIR = path.join(VIEWER_DIR, 'processed');

app.get('/', (req, res) => res.sendFile(path.join(VIEWER_DIR, 'index.html')));
app.get('/api/metadata', (req, res) => res.sendFile(path.join(PROCESSED_DIR, 'metadata.json')));
app.get('/utsw-logo.svg', (req, res) => res.sendFile(path.join(VIEWER_DIR, 'utsw-logo.svg')));
app.use('/images', express.static(PROCESSED_DIR, { maxAge: '1d' }));

app.listen(PORT, () => {
  console.log(`MRI Viewer running at http://localhost:${PORT}`);
});
