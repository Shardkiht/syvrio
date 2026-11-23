import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { Markdown } from './Markdown';

const TABLE = [
  '| 字段 | 值 | 说明 |',
  '| --- | --- | --- |',
  '| USER_STATUS | 1 | 正常 |',
  '| XT36 | 在矫 | 矫正状态 |',
].join('\n');

describe('Markdown', () => {
  it('renders GFM tables with header and body cells', () => {
    const { container } = render(<Markdown text={TABLE} />);
    const table = container.querySelector('.table-wrap > table');
    expect(table).not.toBeNull();
    expect(container.querySelectorAll('thead th')).toHaveLength(3);
    expect(container.querySelectorAll('tbody tr')).toHaveLength(2);
    expect(screen.getByText('USER_STATUS')).toBeTruthy();
    expect(screen.getByText('在矫')).toBeTruthy();
    // The raw pipe source must not survive as literal text.
    expect(container.textContent).not.toContain('| --- |');
  });

  it('keeps tables inside a horizontally scrollable wrapper', () => {
    const { container } = render(<Markdown text={TABLE} />);
    const wrap = container.querySelector('.table-wrap');
    expect(wrap?.tagName).toBe('DIV');
    // A table is never a bare child of .markdown: it always has the scroller.
    expect(container.querySelector('.markdown > table')).toBeNull();
  });

  it('renders tables mixed with paragraphs and code', () => {
    const text = ['说明如下：', '', TABLE, '', '结束。'].join('\n');
    const { container } = render(<Markdown text={text} />);
    expect(container.querySelectorAll('p')).toHaveLength(2);
    expect(container.querySelector('.table-wrap')).not.toBeNull();
  });

  it('supports other GFM marks such as strikethrough', () => {
    const { container } = render(<Markdown text="旧值 ~~已废弃~~ 新值" />);
    expect(container.querySelector('del')?.textContent).toBe('已废弃');
  });

  it('uses the chat thumbnail and viewer for Markdown images, including updates during a reply', () => {
    const { rerender } = render(<Markdown text="![Architecture](/diagram.png)" />);
    const thumb = screen.getByRole('button', { name: 'Preview Architecture' });
    expect(thumb.className).toBe('message-image');
    fireEvent.click(thumb);
    rerender(<Markdown text={'![Architecture](/diagram.png)\n\nMore text'} />);
    expect(screen.getByRole('img', { name: 'Architecture' }).getAttribute('src')).toBe('/diagram.png');
    fireEvent.click(screen.getByRole('button', { name: 'Actual size' }));
    expect(screen.getByRole('region', { name: 'Scrollable image' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Fit to screen' }));
    expect(screen.queryByRole('region', { name: 'Scrollable image' })).toBeNull();
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
    expect(document.activeElement).toBe(thumb);
  });

  it('keeps linked images navigable and read-only documents free of image requests', () => {
    const text = '[![Diagram](/diagram.png)](/guide)';
    const { rerender, container } = render(<Markdown text={text} />);
    const link = screen.getByRole('link', { name: 'Diagram' });
    expect(link.getAttribute('href')).toBe('/guide');
    expect(link.querySelector('.message-image img')).toBeTruthy();
    expect(link.querySelector('button')).toBeNull();
    rerender(<Markdown text={text} reading />);
    expect(container.querySelector('img')).toBeNull();
  });

  it('still drops raw HTML instead of executing it', () => {
    const { container } = render(<Markdown text={'<img src=x onerror="alert(1)">'} />);
    // remark-gfm does not enable raw HTML parsing: the node is dropped entirely,
    // so neither the element nor its handler can reach the DOM.
    expect(container.querySelector('img')).toBeNull();
    expect(container.innerHTML).not.toContain('onerror');
  });
});
