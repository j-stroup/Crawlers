<?php
require __DIR__ . '/lib.php';
header('Content-Type: application/json');

if (($_SERVER['REQUEST_METHOD'] ?? 'GET') !== 'POST') {
    http_response_code(405);
    echo json_encode(['error' => 'POST only']);
    exit;
}

$id = $_POST['id'] ?? '';
if (!preg_match('/^[a-f0-9]{8,32}$/', $id)) {
    http_response_code(400);
    echo json_encode(['error' => 'bad id']);
    exit;
}
if (!find_id_exists($id)) {
    http_response_code(404);
    echo json_encode(['error' => 'unknown id']);
    exit;
}

try {
    echo json_encode(toggle_like($id));
} catch (Throwable $e) {
    http_response_code(500);
    echo json_encode(['error' => 'server']);
}
