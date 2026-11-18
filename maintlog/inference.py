"""Exact joint linear-inference check over exact group statistics.

An audience holds several exact group reports of the form
``sum(x[member] for member in group) = count``.  Two or more overlapping
reports can jointly pin an individual technician's count even when no single
report is a singleton (R1={a,b,c} and R2={a,b} together pin c by subtraction).

The adversary's system is a 0/1 incidence matrix A with integer right-hand
sides.  With non-negative integer variables, a variable x_j is uniquely
determined by an *exact* system iff the unit vector e_j lies in the row space
of A, i.e. iff reduced row echelon form contains a row with a single non-zero
entry in column j.  All arithmetic is exact (fractions.Fraction), so there is
no floating-point rounding, and each pinned variable is returned with the
exact rational combination of held reports that derives it — auditable proof
of the inference.
"""
from fractions import Fraction

def rref(matrix):
    """Return (RREF, combos).

    matrix is a list of rows (lists of Fractions).  combos[i] records the
    exact linear combination of *original* rows that produced RREF row i
    (each combo is a dict {original_row_index: Fraction coefficient}).
    """
    m=[[Fraction(v) for v in row] for row in matrix]
    combos=[{i:Fraction(1)} for i in range(len(m))]
    if not m:return m,combos
    rows,cols=len(m),len(m[0]);r=0
    for c in range(cols):
        piv=next((i for i in range(r,rows) if m[i][c]!=0),None)
        if piv is None:continue
        m[r],m[piv]=m[piv],m[r];combos[r],combos[piv]=combos[piv],combos[r]
        d=m[r][c]
        m[r]=[v/d for v in m[r]]
        combos[r]={k:v/d for k,v in combos[r].items()}
        for i in range(rows):
            if i!=r and m[i][c]!=0:
                f=m[i][c]
                m[i]=[v-f*u for v,u in zip(m[i],m[r])]
                sub={k:f*v for k,v in combos[r].items()}
                merged=dict(combos[i])
                for k,v in sub.items():merged[k]=merged.get(k,Fraction(0))-v
                combos[i]={k:v for k,v in merged.items() if v!=0}
        r+=1
        if r==rows:break
    return m,combos

def inference_audit(columns,constraints):
    """Check whether any individual column is uniquely determined.

    columns: ordered list of technician identifiers (the unknowns x_j).
    constraints: list of dicts {'report_id', 'members' (subset of columns),
        'count' (int; may be None when only the incidence structure matters)}.

    Returns {'pinned': {technician: evidence_dict}, 'rank': int,
             'constraints': canonical constraint list}.  Duplicate incidence
    rows are collapsed: repeating a group the audience already holds adds no
    new equation and cannot be the source of a new inference.
    """
    idx={name:j for j,name in enumerate(columns)}
    seen=set();rows=[];canon=[]
    for c in constraints:
        members=tuple(sorted(c['members']))
        if members in seen:continue
        seen.add(members)
        row=[0]*len(columns)
        for name in members:
            if name not in idx:raise ValueError('unknown_member:'+str(name))
            row[idx[name]]=1
        rows.append(row)
        canon.append({'report_id':c['report_id'],'members':list(members),'count':c.get('count')})
    reduced,combos=rref(rows)
    pinned={}
    for i,row in enumerate(reduced):
        nz=[j for j,v in enumerate(row) if v!=0]
        if len(nz)==1:  # unit row => x_j is uniquely determined
            j=nz[0];coef=combos[i]
            derivation=[{'report_id':canon[k]['report_id'],'coefficient':int(coef[k]) if coef[k].denominator==1 else float(coef[k])} for k in sorted(coef)]
            pinned[columns[j]]={'derived_from':derivation,'rank_row':i}
    rank=sum(1 for row in reduced if any(v!=0 for v in row))
    return {'pinned':pinned,'rank':rank,'constraints':canon}
