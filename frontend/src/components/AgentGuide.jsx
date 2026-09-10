import React from 'react';

function inlineContent(value) {
  return String(value).split(/(`[^`]+`|\*\*[^*]+\*\*)/g).filter(Boolean).map((part, index) => {
    if (part.startsWith('`')) return <code key={index}>{part.slice(1, -1)}</code>;
    if (part.startsWith('**')) return <strong key={index}>{part.slice(2, -2)}</strong>;
    return <React.Fragment key={index}>{part}</React.Fragment>;
  });
}

function tableCells(line) {
  return line.trim().replace(/^\||\|$/g, '').split('|').map(cell => cell.trim());
}

function isTableDivider(line) {
  return /^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?$/.test(line.trim());
}

function startsBlock(line) {
  return /^#{1,3}\s+|^```|^>\s?|^[-*]\s+|^\d+\.\s+|^\|/.test(line);
}

function parseMarkdown(markdown) {
  const lines = String(markdown || '').replace(/\r/g, '').split('\n');
  const blocks = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) { index += 1; continue; }

    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    if (heading) { blocks.push({ type: 'heading', level: heading[1].length, value: heading[2] }); index += 1; continue; }

    if (line.startsWith('```')) {
      const code = [];
      index += 1;
      while (index < lines.length && !lines[index].startsWith('```')) { code.push(lines[index]); index += 1; }
      if (index < lines.length) index += 1;
      blocks.push({ type: 'code', value: code.join('\n') });
      continue;
    }

    if (line.startsWith('|') && index + 1 < lines.length && isTableDivider(lines[index + 1])) {
      const header = tableCells(line);
      const rows = [];
      index += 2;
      while (index < lines.length && lines[index].startsWith('|')) { rows.push(tableCells(lines[index])); index += 1; }
      blocks.push({ type: 'table', header, rows });
      continue;
    }

    const listMatch = line.match(/^([-*]|\d+\.)\s+(.+)$/);
    if (listMatch) {
      const ordered = /\d+\./.test(listMatch[1]);
      const items = [];
      while (index < lines.length) {
        const item = lines[index].match(ordered ? /^\d+\.\s+(.+)$/ : /^[-*]\s+(.+)$/);
        if (!item) break;
        items.push(item[1]);
        index += 1;
      }
      blocks.push({ type: 'list', ordered, items });
      continue;
    }

    if (line.startsWith('>')) {
      const quote = [];
      while (index < lines.length && lines[index].startsWith('>')) { quote.push(lines[index].replace(/^>\s?/, '')); index += 1; }
      blocks.push({ type: 'quote', value: quote.join(' ') });
      continue;
    }

    const paragraph = [line];
    index += 1;
    while (index < lines.length && lines[index].trim() && !startsBlock(lines[index])) { paragraph.push(lines[index]); index += 1; }
    blocks.push({ type: 'paragraph', value: paragraph.join(' ') });
  }
  return blocks;
}

export function AgentGuide({ content }) {
  return <div className="agent-guide-content">{parseMarkdown(content).map((block, index) => {
    if (block.type === 'heading') {
      const Tag = `h${Math.min(block.level + 1, 4)}`;
      return <Tag key={index}>{inlineContent(block.value)}</Tag>;
    }
    if (block.type === 'code') return <pre className="agent-guide-code" key={index}><code>{block.value}</code></pre>;
    if (block.type === 'list') {
      const Tag = block.ordered ? 'ol' : 'ul';
      return <Tag key={index}>{block.items.map((item, itemIndex) => <li key={itemIndex}>{inlineContent(item)}</li>)}</Tag>;
    }
    if (block.type === 'quote') return <blockquote key={index}>{inlineContent(block.value)}</blockquote>;
    if (block.type === 'table') return <div className="agent-guide-table-wrap" key={index}><table className="agent-guide-table"><thead><tr>{block.header.map((cell, cellIndex) => <th key={cellIndex}>{inlineContent(cell)}</th>)}</tr></thead><tbody>{block.rows.map((row, rowIndex) => <tr key={rowIndex}>{block.header.map((_, cellIndex) => <td key={cellIndex}>{inlineContent(row[cellIndex] || '')}</td>)}</tr>)}</tbody></table></div>;
    return <p key={index}>{inlineContent(block.value)}</p>;
  })}</div>;
}
