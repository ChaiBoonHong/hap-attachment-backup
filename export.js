// HAP attachment backup - MongoDB export (read-only).
// Runs inside the HAP MongoDB container with the `mongo` or `mongosh` shell.
// Prints one line per file reference in an attachment field of an active record:
//   HAPBK<TAB>{json}
// and debug details (shown with --debug):
//   HAPBK_INFO<TAB>{json}
// Nothing in MongoDB is changed.

var W = db.getSiblingDB("mdworksheet");   // worksheets and fields
var R = db.getSiblingDB("mdwsrows");      // records, one collection per worksheet: ws<worksheetId>
var A = db.getSiblingDB("mdattachment");  // file details: original name, bucket, storage path
var P = db.getSiblingDB("mdapps");        // app names

var apps = {};
P.apk.find({}, { apkid: 1, apkName: 1 }).forEach(function (a) { apps[a.apkid] = a.apkName; });

var wsCache = {}, titleCache = {};
var stats = { attachmentFields: 0, fieldsSkipped: 0, records: 0, references: 0, withoutFileDetails: 0 };

function info(o) { print("HAPBK_INFO\t" + JSON.stringify(o)); }

function worksheet(id) {
  if (!(id in wsCache)) {
    var w = null;
    try { w = W.worksheet.findOne({ _id: ObjectId(String(id)) }); } catch (e) {}
    if (!w) { w = W.worksheet.findOne({ _id: id }); }
    wsCache[id] = w;
  }
  return wsCache[id];
}

function titleField(wsid) {
  if (!(wsid in titleCache)) {
    var t = W.wscontrols.findOne({ wsid: wsid, attribute: 1 });
    titleCache[wsid] = t ? t.cid : null;
  }
  return titleCache[wsid];
}

function asText(v) {
  if (v === null || v === undefined) { return ""; }
  if (v instanceof Date) { return v.toISOString(); }
  return (typeof v === "string") ? v : JSON.stringify(v);
}

function asNumber(v) {
  if (v === null || v === undefined) { return 0; }
  if (typeof v.toNumber === "function") { return v.toNumber(); }
  return Number(v);
}

W.wscontrols.find({ type: 14 }).forEach(function (c) {           // type 14 = attachment field
  stats.attachmentFields++;
  var w = worksheet(c.wsid);
  if (!w) {
    stats.fieldsSkipped++;
    info({ skipped: "worksheet not found (deleted?)", worksheetId: String(c.wsid), field: c.name });
    return;
  }
  var tcid = titleField(c.wsid);
  if (!tcid) { info({ note: "worksheet has no title field, record IDs are used", worksheet: w.name }); }
  var q = { status: 1 };                                        // active records only (9 = recycle bin)
  q[c.cid] = { $exists: true, $nin: [null, "", "[]"] };
  var before = stats.references;
  R.getCollection("ws" + c.wsid).find(q).forEach(function (d) {
    stats.records++;
    var v = d[c.cid];
    if (typeof v === "string") { try { v = JSON.parse(v); } catch (e) { v = []; } }
    if (!Array.isArray(v)) { v = []; }
    v.forEach(function (f) {
      var fid = (typeof f === "string") ? f : (f && (f.fileID || f.fileId || f.fid));
      if (!fid) { return; }
      stats.references++;
      var o = {
        fid: fid,
        appId: w.appid,
        app: apps[w.appid] || w.appid,
        wsId: String(c.wsid),
        ws: w.name,
        fieldId: c.cid,
        field: c.name,
        rowId: d.rowid,
        title: tcid ? asText(d[tcid]) : ""
      };
      var a = A.attachment.findOne({ fid: fid });
      if (a) {
        o.server = a.server;
        o.path = a.path;
        o.name = a.name;
        o.oname = a.oFilename;
        o.size = asNumber(a.size);
        o.isDel = !!a.isDel;
        o.ctime = a.ctime ? asText(a.ctime) : "";
      } else {
        stats.withoutFileDetails++;
      }
      print("HAPBK\t" + JSON.stringify(o));
    });
  });
  info({ field: c.name, worksheet: w.name, app: apps[w.appid] || w.appid, references: stats.references - before });
});

info(stats);
