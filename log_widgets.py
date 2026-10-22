"""Incremental plain-text event logs with stable selection and a reading anchor."""
from PySide6.QtGui import QTextCursor


def format_events(events,previous,format_time):
    """Reuse unchanged event labels; release entries absent from this snapshot."""
    labels={};lines=[]
    for event in events:
        key=(event['ts'],event['message']);line=previous.get(key)
        if line is None:line=format_time(event['ts'])+' · '+event['message']
        labels[key]=line;lines.append(line)
    return lines,labels


def suffix_overlap(old,new):
    """Longest old suffix matching a new prefix, linear even for repeated lines."""
    if not old or not new:return 0
    prefix=[0]*len(new);matched=0
    for index in range(1,len(new)):
        while matched and new[index]!=new[matched]:matched=prefix[matched-1]
        if new[index]==new[matched]:matched+=1
        prefix[index]=matched
    matched=0
    for line in old:
        while matched and (matched==len(new) or line!=new[matched]):matched=prefix[matched-1]
        if matched<len(new) and line==new[matched]:matched+=1
    return matched


def update_lines(editor,old,lines):
    """Only replace a document when no surviving history connects the snapshots."""
    if old==lines:return lines
    bar=editor.verticalScrollBar();follow=bar.value()>=bar.maximum()-1
    block=editor.firstVisibleBlock();anchor=QTextCursor(block)
    offset=max(0,bar.value()-block.firstLineNumber());selection=editor.textCursor()
    overlap=len(old) if lines[:len(old)]==old else suffix_overlap(old,lines)
    if overlap or not old:
        removed=len(old)-overlap
        if removed:
            cursor=QTextCursor(editor.document());cursor.movePosition(QTextCursor.MoveOperation.Start)
            # Find the endpoint in the document tree instead of walking every
            # expired block. One error event may contain several Qt paragraphs;
            # CRLF is one boundary, and non-BMP characters need no offset math.
            blocks=sum(1+line.count('\n')+line.count('\r')-line.count('\r\n')+line.count('\u2029') for line in old[:removed])
            cursor.setPosition(editor.document().findBlockByNumber(blocks).position(),QTextCursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()
        if lines[overlap:]:editor.appendPlainText('\n'.join(lines[overlap:]))
        editor.setTextCursor(selection)
        # QPlainTextEdit scrolls in visual lines, not source paragraphs. The
        # tracked block survives prefix removal, including word-wrapped events.
        bar.setValue(bar.maximum() if follow else max(0,anchor.block().firstLineNumber()+offset))
    else:
        position=bar.value();editor.setPlainText('\n'.join(lines))
        bar.setValue(bar.maximum() if follow else position)
    return lines
