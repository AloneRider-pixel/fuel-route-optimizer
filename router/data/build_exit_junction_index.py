import osmium, json, sys
st, pbf = sys.argv[1], sys.argv[2]
class Ways(osmium.SimpleHandler):
    def __init__(self): super().__init__(); self.w={}
    def way(self, w):
        if w.tags.get('highway') in ('motorway','trunk'):
            ref = w.tags.get('ref')
            if ref: self.w[w.id]=[r.strip() for r in ref.split(';')]
class J(osmium.SimpleHandler):
    def __init__(self): super().__init__(); self.out={}
    def node(self, n):
        if n.tags.get('highway')=='motorway_junction' and n.location.valid():
            self.out[n.id]={'lat':n.location.lat,'lon':n.location.lon,'ref':n.tags.get('ref','')}
class B(osmium.SimpleHandler):
    def __init__(self, ways, j): super().__init__(); self.ways=ways; self.j=j; self.idx={}
    def way(self, w):
        refs = self.ways.get(w.id)
        if not refs: return
        for nd in w.nodes:
            e = self.j.get(nd.ref)
            if e:
                for r in refs:
                    key=(r.upper().replace(' ','').replace('-',''), e['ref'])
                    self.idx.setdefault(key, []).append([round(e['lat'],6), round(e['lon'],6), nd.ref])
waysh = Ways(); waysh.apply_file(pbf, locations=False)
jh = J(); jh.apply_file(pbf, locations=True)
bh = B(waysh.w, jh.out); bh.apply_file(pbf, locations=False)
out={f"{st}|{h}|{e}":{'nodes':v} for (h,e),v in bh.idx.items()}
json.dump(out, open(f'junctions_{st}.json','w'))
print(st, len(out), 'keys', len(jh.out), 'junction nodes')
