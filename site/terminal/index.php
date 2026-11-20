<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>System Terminal</title>

    <link href="static/css/styles.css" rel="stylesheet">

    <body onclick="document.getElementById('cmd').focus()">
        <div id="terminal-container">
            <div id="history">Welcome to the terminal!</div>
            <div class="input-row">
                <span id="prompt">guest@system:~$</span>
                <input type="text" id="cmd" autofocus autocomplete="off">
            </div>
        </div>
<script src="./static/js/terminal.js"></script>
</body>
</html>