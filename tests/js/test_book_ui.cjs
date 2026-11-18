'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { containRect, mapClickToSource, boxToDisplay, hitDetection, sameContext, responseMatches, shouldResume } = require('../../app/book_ui.js');

test('contain ignores vertical and horizontal letterboxes', () => {
  const wide = containRect({ left: 20, top: 10, width: 800, height: 600 }, 1600, 900);
  assert.deepEqual(wide, { left: 20, top: 85, width: 800, height: 450, scale: .5 });
  assert.equal(mapClickToSource({ x: 200, y: 40 }, { left: 20, top: 10, width: 800, height: 600 }, 1600, 900), null);
  const tallContainer = { left: 100, top: 50, width: 800, height: 600 };
  assert.equal(mapClickToSource({ x: 150, y: 300 }, tallContainer, 600, 1200), null);
  assert.deepEqual(mapClickToSource({ x: 500, y: 350 }, tallContainer, 600, 1200), { x: 300, y: 600 });
});

test('scaling and offsets map the same point back to source pixels', () => {
  const rect = { left: 80, top: 30, width: 640, height: 480 };
  assert.deepEqual(mapClickToSource({ x: 400, y: 270 }, rect, 1280, 720), { x: 640, y: 360 });
  const display = boxToDisplay([320, 180, 960, 540], { coordinate_space: 'pixel', reference_width: 1280, reference_height: 720 }, rect);
  assert.deepEqual(display, { left: 240, top: 180, width: 320, height: 180 });
});

test('a 600-pixel box stays pixel based and the smallest containing object wins', () => {
  const contract = { coordinate_space: 'pixel', reference_width: 864, reference_height: 480 };
  assert.deepEqual(boxToDisplay([100, 100, 600, 400], contract, { width: 864, height: 480 }), { left: 100, top: 100, width: 500, height: 300 });
  assert.equal(boxToDisplay([0, 0, 1000, 1000], { coordinate_space: 'normalized_1000', reference_width: 864, reference_height: 480 }, { width: 864, height: 480 }), null);
  const rows = [{ label: 'forest', box: [0, 0, 864, 480] }, { label: 'rabbit', box: [100, 100, 600, 400] }];
  assert.equal(hitDetection(rows, { x: 300, y: 200 }).label, 'rabbit');
  assert.equal(hitDetection(rows, { x: 900, y: 200 }), null);
});

const snapshot = { bookId: 'book1', pageId: 'p0001', mediaPath: '/clip.mp4|/audio.wav', epoch: 9, source: 'video', frameTime: 1.25, wasPlaying: true, requestId: 'request1' };
const response = { book_id: 'book1', page_id: 'p0001', request_id: 'request1' };

test('late recognition is rejected after page/book/media/seek/replay changes', () => {
  assert.equal(responseMatches(snapshot, { ...snapshot }, response), true);
  for (const changed of [{ pageId: 'p0002' }, { bookId: 'book2' }, { mediaPath: '/other.mp4' }, { epoch: 10 }, { frameTime: 2 }]) {
    assert.equal(sameContext(snapshot, { ...snapshot, ...changed }), false);
    assert.equal(responseMatches(snapshot, { ...snapshot, ...changed }, response), false);
  }
  assert.equal(responseMatches(snapshot, snapshot, { ...response, request_id: 'older-request' }), false);
  assert.equal(responseMatches(snapshot, snapshot, { ...response, page_id: 'p0002' }), false);
});

test('resume requires unchanged media, previously playing, and successful uncancelled speech', () => {
  assert.equal(shouldResume(snapshot, { ...snapshot }), true);
  assert.equal(shouldResume({ ...snapshot, wasPlaying: false }, snapshot), false);
  assert.equal(shouldResume(snapshot, { ...snapshot, epoch: 10 }), false);
  assert.equal(shouldResume(snapshot, snapshot, { cancelled: true }), false);
  assert.equal(shouldResume(snapshot, snapshot, { playbackRejected: true }), false);
  assert.equal(shouldResume(snapshot, { ...snapshot, frameTime: 2 }), false);
});

const { pageAtTime, sentenceAt, momentTarget, frameKey, splitLabel, median } = require('../../app/book_ui.js');

test('film clock maps to pages by quantized visual boundaries', () => {
  const timeline = [{ page_id: 'p0001', book_start: 0, book_end: 1.01, video_start: 0, video_end: 1.0 },
    { page_id: 'p0002', book_start: 1.01, book_end: 2.5, video_start: 1.0, video_end: 2.5 }];
  assert.equal(pageAtTime(timeline, 0), 0);
  assert.equal(pageAtTime(timeline, 1.005), 1);
  assert.equal(pageAtTime(timeline, 2.5), 1);
  assert.equal(pageAtTime(timeline, -1), -1);
  assert.equal(pageAtTime([], 1), -1);
  const rows = [{ text: 'a', book_start: 0, book_end: 1 }, { text: 'b', book_start: 1, book_end: 2 }];
  assert.equal(sentenceAt(rows, 1.2, 'book_start', 'book_end').text, 'b');
  assert.equal(sentenceAt(rows, 3, 'book_start', 'book_end'), null);
});

test('moments play on the film clock only when the film exists', () => {
  const moment = { page_id: 'p0002', start: .4, book_start: 1.4 };
  assert.deepEqual(momentTarget(moment, true), { mode: 'film', time: 1.4 });
  assert.deepEqual(momentTarget(moment, false), { mode: 'page', pageId: 'p0002', time: .4 });
  assert.deepEqual(momentTarget({ page_id: 'p0003', start: null, book_start: null }, true), { mode: 'page', pageId: 'p0003', time: null });
  assert.equal(momentTarget({}, true), null);
});

test('frame keys ignore epoch but change with media, page and frame time', () => {
  const ctx = { bookId: 'b', pageId: 'p0001', source: 'film', mediaPath: 'film:/x.mp4', frameTime: 1.25, epoch: 1 };
  assert.equal(frameKey(ctx), frameKey({ ...ctx, epoch: 9 }));
  assert.notEqual(frameKey(ctx), frameKey({ ...ctx, frameTime: 1.3 }));
  assert.notEqual(frameKey(ctx), frameKey({ ...ctx, pageId: 'p0002' }));
  assert.equal(frameKey({ ...ctx, source: 'image', frameTime: 4 }), frameKey({ ...ctx, source: 'image', frameTime: 0 }));
  assert.equal(frameKey({ ...ctx, pageId: '' }), '');
});

test('film frames are compared by time like page clips', () => {
  const film = { bookId: 'b', pageId: 'p1', mediaPath: 'film:/x', epoch: 1, source: 'film', frameTime: 3 };
  assert.equal(sameContext(film, { ...film }), true);
  assert.equal(sameContext(film, { ...film, frameTime: 3.5 }), false);
  assert.equal(sameContext(film, { ...film, source: 'video' }), false);
});

test('bilingual labels display both names and median is robust', () => {
  assert.deepEqual(splitLabel('小兔子<rabbit>'), { zh: '小兔子', en: 'rabbit', display: '小兔子 · rabbit' });
  assert.deepEqual(splitLabel('苹果'), { zh: '苹果', en: '', display: '苹果' });
  assert.deepEqual(splitLabel('<apple>'), { zh: 'apple', en: 'apple', display: 'apple' });
  assert.equal(median([5, 1, 3]), 3);
  assert.equal(median([4, 1, 3, 2]), 2.5);
  assert.equal(median([]), null);
});

test('export readiness lists every page still missing narration or animation', () => {
  const { mediaReadiness } = require('../../app/book_ui.js');
  const page = (num, tts, video) => ({ page_num: num, media: { tts: { status: tts }, video: { status: video } } });
  assert.deepEqual(mediaReadiness([page(1, 'not_requested', 'not_requested'), page(2, 'ready', 'ready'), page(3, 'silent', 'ready'), page(4, 'ready', 'failed')]),
    { total: 4, ready: 2, missing: [1, 4] });
  assert.deepEqual(mediaReadiness(undefined), { total: 0, ready: 0, missing: [] });
});
