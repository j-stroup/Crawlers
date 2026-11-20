<?php
// Edit these for your deployment.
return [
    // The redacted export produced by:  comment_hunter.py seeds.txt --public
    'data_file' => __DIR__ . '/data/public_finds.json',
    // SQLite file for likes (the web user must be able to write to data/).
    'db_file'   => __DIR__ . '/data/votes.sqlite',
    // Shown in the footer so site owners can ask for removal.
    // NOTE: verify this mailbox actually exists before going live.
    'contact'   => 'hello@v0rt.com',
    'site_name' => 'v0rt',
    // SEO / analytics (optional - leave blank to omit):
    //   ga_id:      Google Analytics 4 measurement id, e.g. 'G-XXXXXXXXXX'
    //   gsc_verify: the content value from Google Search Console's HTML-tag
    //               verification method (just the token, not the whole tag)
    'ga_id'      => 'G-9HVVWXS0DT',
    'gsc_verify' => '',
];
