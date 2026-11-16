using System.Diagnostics;
using System.IO.Pipes;
using System.Text.Json;
using System.Text.Json.Serialization;

// One relative mouse state per JSON line. dt_ms is the delay BEFORE this state.
[JsonUnmappedMemberHandling(JsonUnmappedMemberHandling.Disallow)]
public sealed class MouseFrame {
 [JsonPropertyName("dx")]public required int Dx {get;init;}
 [JsonPropertyName("dy")]public required int Dy {get;init;}
 [JsonPropertyName("dt_ms")]public int Delay {get;init;}
 [JsonPropertyName("buttons")]public int Buttons {get;init;}
 [JsonPropertyName("wheel")]public int Wheel {get;init;}
 [JsonPropertyName("pan")]public int Pan {get;init;}
 public void Validate(){
  if(Dx< -32767||Dx>32767||Dy< -32767||Dy>32767)throw new ArgumentException("dx/dy must be -32767..32767 counts per report.");
  if(Delay<0||Delay>60000)throw new ArgumentException("dt_ms must be 0..60000.");
  if(Buttons<0||Buttons>31)throw new ArgumentException("buttons must be 0..31.");
  if(Wheel< -127||Wheel>127||Pan< -127||Pan>127)throw new ArgumentException("wheel/pan must be -127..127 detents.");
 }
 // The current descriptor: 5 buttons + 3 padding bits, signed little-endian
 // 16-bit relative X/Y, signed 8-bit vertical/horizontal wheel, no Report ID.
 public byte[] HidBytes()=>[(byte)Buttons,unchecked((byte)Dx),unchecked((byte)(Dx>>8)),unchecked((byte)Dy),unchecked((byte)(Dy>>8)),unchecked((byte)Wheel),unchecked((byte)Pan)];
}
static class MouseStream {
 public static int Run(string[] args){
  if(args.Length<2)throw new ArgumentException("stream <file.jsonl|-> [--audit] [--dry-run]");
  bool audit=args.Contains("--audit"),dry=args.Contains("--dry-run");
  if(args.Skip(2).Any(x=>x!="--audit"&&x!="--dry-run")||dry&&audit)throw new ArgumentException("Use --audit OR --dry-run.");
  using TextReader input=args[1]=="-"?Console.In:new StreamReader(args[1]);
  NamedPipeClientStream? pipe=null;StreamReader? reader=null;StreamWriter? writer=null;
  string Send(params string[] a){
   writer!.WriteLine(JsonSerializer.Serialize(a));
   using var timeout=new CancellationTokenSource(5000);
   string reply=reader!.ReadLineAsync(timeout.Token).AsTask().GetAwaiter().GetResult()??"ERROR: disconnected";
   if(!reply.StartsWith("OK"))throw new IOException(reply);return reply;
  }
  long sx=0,sy=0;int count=0,lineNumber=0;bool auditStarted=false;
  var watch=Stopwatch.StartNew();
  try {
   if(!dry){pipe=new NamedPipeClientStream(".",Program.Pipe,PipeDirection.InOut,PipeOptions.Asynchronous);pipe.Connect(3000);reader=new StreamReader(pipe);writer=new StreamWriter(pipe){AutoFlush=true};if(audit){Send("audit-start");auditStarted=true;}}
   string? line;
   while((line=input.ReadLine())!=null){
    lineNumber++;if(string.IsNullOrWhiteSpace(line))continue;
    MouseFrame frame;
    try{frame=JsonSerializer.Deserialize<MouseFrame>(line)??throw new ArgumentException("Null frame.");frame.Validate();}catch(Exception ex){throw new ArgumentException($"Line {lineNumber}: {ex.Message}",ex);}
    if(dry)Console.WriteLine(Convert.ToHexString(frame.HidBytes()));
    else{if(frame.Delay>0)Thread.Sleep(frame.Delay);Send("mouse",JsonSerializer.Serialize(frame));}
    count++;sx+=frame.Dx;sy+=frame.Dy;
   }
   if(!dry)Send("mouse",JsonSerializer.Serialize(new MouseFrame{Dx=0,Dy=0}));
   object? received=null;bool? passed=null;
   if(audit){Thread.Sleep(200);string reply=Send("audit-stop");auditStarted=false;
    using var result=JsonDocument.Parse(reply[3..]);received=result.RootElement.Clone();
    passed=count>0&&result.RootElement.GetProperty("events").GetInt32()>0&&result.RootElement.GetProperty("dx").GetInt64()==sx&&result.RootElement.GetProperty("dy").GetInt64()==sy;
   }
   string report=JsonSerializer.Serialize(new{mode=dry?"HID-byte-preview":"driver-backed-HID",frames=count,sent_dx=sx,sent_dy=sy,raw_input=received,passed,elapsed_ms=watch.Elapsed.TotalMilliseconds},new JsonSerializerOptions{WriteIndented=true});
   Console.WriteLine(report);
   if(audit)File.WriteAllText(Path.Combine(Program.Folder,"stream-verification.json"),report);
   return passed==false?2:0;
  }finally{
   if(writer!=null){try{Send("mouse",JsonSerializer.Serialize(new MouseFrame{Dx=0,Dy=0}));}catch{}if(auditStarted)try{Send("audit-stop");}catch{}}
   writer?.Dispose();reader?.Dispose();pipe?.Dispose();
  }
 }
}
