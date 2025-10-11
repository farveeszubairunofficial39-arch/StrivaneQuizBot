import re
question_text = m.group('question').strip()
opts = [m.group('A').strip(), m.group('B').strip(), m.group('C').strip(), m.group('D').strip()]
ans = m.group('answer')
if ans is None:
# fallback: try to detect answer in-line like "Answer: C" or end with (C)
correct = 0
else:
correct = int(ans) - 1
neg = m.group('negative')
if neg is None:
negative = default_negative
else:
negative = _parse_number_expr(neg)
time_s = m.group('time')
if time_s is None:
time_v = default_time
else:
time_v = int(time_s)


questions.append(Question(qid=qid, text=question_text, options=opts, correct_option=correct, negative=negative, time=time_v))


# If no matches by regex, try a simpler fallback parser (less strict) — split by double blank lines
if not questions:
blocks = [b.strip() for b in t.split('\n\n') if b.strip()]
qnum = 1
for b in blocks:
lines = [l.strip() for l in b.split('\n') if l.strip()]
if len(lines) >= 5:
# assume first line is question, next 4 are options
qtext = lines[0]
opts = [lines[1][2:].strip() if lines[1].startswith(('A.', 'A)')) else lines[1],
lines[2][2:].strip() if lines[2].startswith(('B.', 'B)')) else lines[2],
lines[3][2:].strip() if lines[3].startswith(('C.', 'C)')) else lines[3],
lines[4][2:].strip() if lines[4].startswith(('D.', 'D)')) else lines[4]]
# try to find Answer: line
ans = None
neg = None
time_v = default_time
for line in lines[5:]:
if line.lower().startswith('answer:'):
try:
ans = int(line.split(':', 1)[1].strip()) - 1
except Exception:
pass
if line.lower().startswith('negative:'):
try:
neg = _parse_number_expr(line.split(':', 1)[1].strip())
except Exception:
pass
if line.lower().startswith('time:'):
try:
time_v = int(line.split(':', 1)[1].strip())
except Exception:
pass
negative = neg if neg is not None else default_negative
correct = ans if ans is not None else 0
questions.append(Question(qid=qnum, text=qtext, options=opts, correct_option=correct, negative=negative, time=time_v))
qnum += 1


return questions
