using HIDMaestro;
using System.Diagnostics;
using System.IO.Pipes;
using System.Runtime.InteropServices;
using System.Security.Principal;
using System.Security.AccessControl;
using Microsoft.Win32.SafeHandles;
using System.Text.Json;
using System.Text.Json.Serialization;

static class Program {
 public const string Pipe="Colin.VirtualKM.Local.v1";
 public static readonly string Folder=AppContext.BaseDirectory;
 [STAThread] static int Main(string[] args) {
  try {
   if(args.Length>0&&args[0]=="stream")return MouseStream.Run(args);
   if(args.Length>0 && args[0]!="host" && args[0]!="host-mouse" && args[0]!="verify") {
    using var p=new NamedPipeClientStream(".",Pipe,PipeDirection.InOut);p.Connect(3000);
    using var r=new StreamReader(p);using var w=new StreamWriter(p){AutoFlush=true};
    w.WriteLine(JsonSerializer.Serialize(args));string reply=r.ReadLine()??"ERROR: no reply";Console.WriteLine(reply);return reply.StartsWith("OK")?0:1;
   }
   if(!new WindowsPrincipal(WindowsIdentity.GetCurrent()).IsInRole(WindowsBuiltInRole.Administrator)) {
    string script=Path.GetFullPath(Path.Combine(Folder,"..","Start.ps1"));
    Process.Start(new ProcessStartInfo("powershell.exe",$"-NoProfile -ExecutionPolicy Bypass -File \"{script}\""+(args.Length>0&&args[0]=="verify"?" -Verify":"")+(args.Length>0&&args[0]=="host-mouse"?" -MouseOnly":"")){UseShellExecute=true,Verb="runas",WindowStyle=ProcessWindowStyle.Hidden});return 0;
   }
   using var mutex=new Mutex(true,"Local\\Colin.VirtualKM.Host.v1",out bool first);if(!first)throw new Exception("VirtualKM is already running.");
   ApplicationConfiguration.Initialize();Application.Run(new Host(args.Length>0&&args[0]=="verify",args.Length>0&&args[0]=="host-mouse"));return Environment.ExitCode;
  } catch(Exception ex){File.AppendAllText(Path.Combine(Folder,"error.log"),DateTime.Now+" "+ex+Environment.NewLine);Console.Error.WriteLine(ex.Message);return 1;}
 }
}
class Host : Form {
 HMContext? ctx; HMController? keyboard,mouse;
 readonly TextBox log=new(){Multiline=true,ReadOnly=true,Dock=DockStyle.Fill,ScrollBars=ScrollBars.Vertical};
 readonly CancellationTokenSource stop=new();
 readonly System.Windows.Forms.Timer test=new(){Interval=300};
 int phase;bool keyDown,keyUp,mouseRight,mouseLeft;bool testing;bool auto;
 bool auditing;long rawDx,rawDy;int rawMoves;
 readonly bool mouseOnly;
 public Host(bool verify,bool onlyMouse=false) {
  mouseOnly=onlyMouse;
  auto=verify;Text="VirtualKM - local HID keyboard / mouse";Width=720;Height=380;
  if(mouseOnly){ShowInTaskbar=false;Opacity=0;WindowState=FormWindowState.Minimized;}
  var bar=new FlowLayoutPanel{Dock=DockStyle.Top,Height=45};
  var check=new Button{Text="Verify HID",Width=130,Enabled=!mouseOnly};check.Click+=(_,_)=>BeginTest();
  var release=new Button{Text="Release keys",Width=130};release.Click+=(_,_)=>Release();
  var close=new Button{Text="Stop and remove",Width=150};close.Click+=(_,_)=>Close();
  bar.Controls.AddRange([check,release,close]);Controls.Add(log);Controls.Add(bar);
  Shown+=(_,_)=>Initialize();FormClosed+=(_,_)=>{stop.Cancel();test.Stop();keyboard?.Dispose();mouse?.Dispose();ctx?.Dispose();};
  test.Tick+=(_,_)=>TestStep();
 }
 void Log(string s){log.AppendText(s+Environment.NewLine);File.AppendAllText(Path.Combine(Program.Folder,"host.log"),DateTime.Now.ToString("s")+" "+s+Environment.NewLine);}
 void StartupState(string state,string? error=null){
  string path=Path.Combine(Program.Folder,"startup-state.json");
  File.WriteAllText(path+".tmp",JsonSerializer.Serialize(new{state,pid=Environment.ProcessId,time=DateTimeOffset.Now,error}));
  File.Move(path+".tmp",path,true);
 }
 HMProfile Profile(string kind,ushort pid,HidDescriptorBuilder desc)=>new HMProfileBuilder().Id("colin-virtual-"+kind).Name("Colin Virtual "+kind).Vendor("Local development").Vid(0xF055).Pid(pid).ProductString("Colin Virtual "+kind).ManufacturerString("Local development").Type(kind).Connection("usb").FromDescriptorBuilder(desc).Build();
 void Initialize(){
  try {
   StartupState("starting");
   Log("Installing/checking HIDMaestro UMDF driver...");ctx=new HMContext();ctx.InstallDriver();
   if(!mouseOnly)keyboard=ctx.CreateController(Profile("keyboard",1,new HidDescriptorBuilder().Keyboard()),"colin-local-keyboard");
   mouse=ctx.CreateController(Profile("mouse",2,new HidDescriptorBuilder().Mouse()),"colin-local-mouse");
   Raw.Device[] raw=mouseOnly?[new(){Page=1,Usage=2,Flags=0x100,Window=Handle}]:[new(){Page=1,Usage=6,Flags=0x100,Window=Handle},new(){Page=1,Usage=2,Flags=0x100,Window=Handle}];
   if(!Raw.RegisterRawInputDevices(raw,(uint)raw.Length,(uint)Marshal.SizeOf<Raw.Device>()))throw new System.ComponentModel.Win32Exception();
   Log(mouseOnly?"READY: Colin Virtual mouse F055:0002 (mouse-only). No TCP listener.":"READY: Colin Virtual keyboard F055:0001 + mouse F055:0002. No TCP listener.");
   _=Serve();StartupState("ready");if(mouseOnly)Hide();if(auto)BeginTest();
  }catch(Exception ex){
   Log("ERROR: "+ex);StartupState("failed",ex.ToString());
   File.WriteAllText(Path.Combine(Program.Folder,"verification.json"),JsonSerializer.Serialize(new{Passed=false,Error=ex.ToString()}));
   Environment.ExitCode=1;BeginInvoke(Close);
  }
 }
 async Task Serve(){
  while(!stop.IsCancellationRequested) {
   try{
    var sid=WindowsIdentity.GetCurrent().User!;
    var security=new PipeSecurity();security.SetOwner(sid);security.SetAccessRuleProtection(true,false);
    security.AddAccessRule(new PipeAccessRule(sid,PipeAccessRights.FullControl,AccessControlType.Allow));
    using var pipe=NamedPipeServerStreamAcl.Create(Program.Pipe,PipeDirection.InOut,1,PipeTransmissionMode.Byte,PipeOptions.Asynchronous,4096,4096,security);
    await pipe.WaitForConnectionAsync(stop.Token);
    using var r=new StreamReader(pipe);using var w=new StreamWriter(pipe){AutoFlush=true};
    string? line;
    while((line=await r.ReadLineAsync(stop.Token))!=null){
     string reply;
     try{reply=Command(JsonSerializer.Deserialize<string[]>(line)!);}catch(Exception ex){reply="ERROR: "+ex.Message;}
     await w.WriteLineAsync(reply);
    }
   }catch(OperationCanceledException){break;}catch(Exception ex){Log("Pipe: "+ex.Message);}
  }
 }
 string Command(string[] a){
  if(a.Length==0)throw new ArgumentException("status | key <usage> [modifier] | move <x> <y> | click <left/right/middle> | wheel <steps> | release | self-test | stop");
  switch(a[0]) {
   case "status":return "OK: "+(mouseOnly?"mouse-only; ":"keyboard F055:0001; ")+"mouse F055:0002; HIDMaestro 2.0.3 UMDF; current-user local pipe.";
   case "key":if(keyboard==null)throw new InvalidOperationException("Keyboard is not enabled in mouse-only mode.");try{keyboard.SubmitKeyboard((HMKeyModifiers)(a.Length>2?byte.Parse(a[2]):0),new[]{(HMKey)byte.Parse(a[1])});Thread.Sleep(40);}finally{keyboard.SubmitKeyboard(HMKeyModifiers.None,ReadOnlySpan<HMKey>.Empty);}break;
   case "move":mouse!.SubmitMouse(new HMMouseState{X=Bound(a[1]),Y=Bound(a[2])});break;
   case "mouse":
    var frame=JsonSerializer.Deserialize<MouseFrame>(a[1])!;frame.Validate();
    mouse!.SubmitMouse(new HMMouseState{X=frame.Dx,Y=frame.Dy,Wheel=frame.Wheel,Pan=frame.Pan,Buttons=(HMMouseButtons)frame.Buttons});break;
   case "audit-start":if(testing||auditing)throw new InvalidOperationException("A verification is already running.");rawDx=rawDy=0;rawMoves=0;auditing=true;return "OK";
   case "audit-stop":auditing=false;return "OK "+JsonSerializer.Serialize(new{dx=rawDx,dy=rawDy,events=rawMoves});
   case "wheel":mouse!.SubmitMouse(new HMMouseState{Wheel=Bound(a[1])});break;
   case "click":var button=a[1] switch{"left"=>HMMouseButtons.Left,"right"=>HMMouseButtons.Right,"middle"=>HMMouseButtons.Middle,_=>throw new ArgumentException("left/right/middle")};try{mouse!.SubmitMouse(new HMMouseState{Buttons=button});Thread.Sleep(40);}finally{mouse!.SubmitMouse(new HMMouseState());}break;
   case "release":Release();break;
   case "self-test":BeginTest();return "OK: verification started; inspect verification.json after 3 seconds.";
   case "stop":BeginInvoke(Close);return "OK: stopping and removing virtual devices.";
   default:throw new ArgumentException("Unknown command.");
  }
  return "OK";
 }
 int Bound(string s){int n=int.Parse(s);if(n< -10000||n>10000)throw new ArgumentOutOfRangeException(nameof(s));return n;}
 void Release(){keyboard?.SubmitKeyboard(HMKeyModifiers.None,ReadOnlySpan<HMKey>.Empty);mouse?.SubmitMouse(new HMMouseState());}
 void BeginTest(){if(testing||keyboard==null||mouse==null)return;keyDown=keyUp=mouseRight=mouseLeft=false;phase=0;testing=true;Log("Raw Input verification: F24 down/up, mouse +1/-1, no clicks.");test.Start();}
 void TestStep(){try{
  phase++;
  if(phase==1)keyboard!.SubmitKeyboard(HMKeyModifiers.None,new[]{HMKey.F24});
  if(phase==2)keyboard!.SubmitKeyboard(HMKeyModifiers.None,ReadOnlySpan<HMKey>.Empty);
  if(phase==3)mouse!.SubmitMouse(new HMMouseState{X=1});
  if(phase==4)mouse!.SubmitMouse(new HMMouseState{X=-1});
  if(phase==7){test.Stop();testing=false;bool passed=keyDown&&keyUp&&mouseRight&&mouseLeft;File.WriteAllText(Path.Combine(Program.Folder,"verification.json"),JsonSerializer.Serialize(new{Passed=passed,KeyboardDown=keyDown,KeyboardUp=keyUp,MousePositive=mouseRight,MouseNegative=mouseLeft,Time=DateTimeOffset.Now},new JsonSerializerOptions{WriteIndented=true}));Log("VERIFIED="+passed);}
 }catch(Exception ex){test.Stop();testing=false;Log("Verification failed: "+ex);}}
 protected override void WndProc(ref Message m){
  if(m.Msg==0xff&&(testing||auditing)){uint n=0,header=(uint)(8+IntPtr.Size*2);Raw.GetRawInputData(m.LParam,0x10000003,IntPtr.Zero,ref n,header);
   if(n>=header){IntPtr p=Marshal.AllocHGlobal((int)n);try{
    if(Raw.GetRawInputData(m.LParam,0x10000003,p,ref n,header)!=uint.MaxValue){string name=Raw.Name(Marshal.ReadIntPtr(p,8));int type=Marshal.ReadInt32(p);
     if(Raw.Matches(name,1)&&type==1){int offset=(int)header;if((ushort)Marshal.ReadInt16(p,offset+6)==0x87){bool up=(Marshal.ReadInt16(p,offset+2)&1)!=0;if(up)keyUp=true;else keyDown=true;}}
     if(Raw.Matches(name,2)&&type==0){int dx=Marshal.ReadInt32(p,(int)header+12),dy=Marshal.ReadInt32(p,(int)header+16);if(dx==1)mouseRight=true;if(dx== -1)mouseLeft=true;if(auditing){rawDx+=dx;rawDy+=dy;rawMoves++;}}
    }
   }finally{Marshal.FreeHGlobal(p);}}
  }base.WndProc(ref m);
 }
}
static class Raw {
 [StructLayout(LayoutKind.Sequential)]struct Attributes{public int Size;public ushort Vid,Pid,Version;}
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode)]static extern SafeFileHandle CreateFile(string p,uint access,uint share,IntPtr sa,uint create,uint flags,IntPtr t);
 [DllImport("hid.dll")]static extern bool HidD_GetAttributes(SafeFileHandle h,ref Attributes a);
 public static bool Matches(string name,ushort pid){if(name.Length==0)return false;using var h=CreateFile(name,0,3,IntPtr.Zero,3,0,IntPtr.Zero);if(h.IsInvalid)return false;var a=new Attributes{Size=Marshal.SizeOf<Attributes>()};return HidD_GetAttributes(h,ref a)&&a.Vid==0xF055&&a.Pid==pid;}
 [StructLayout(LayoutKind.Sequential)] public struct Device{public ushort Page,Usage;public uint Flags;public IntPtr Window;}
 [DllImport("user32.dll",SetLastError=true)]public static extern bool RegisterRawInputDevices(Device[] devices,uint count,uint size);
 [DllImport("user32.dll")]public static extern uint GetRawInputData(IntPtr h,uint command,IntPtr data,ref uint size,uint header);
 [DllImport("user32.dll",CharSet=CharSet.Unicode)]static extern uint GetRawInputDeviceInfo(IntPtr h,uint command,IntPtr data,ref uint size);
 public static string Name(IntPtr h){uint n=0;GetRawInputDeviceInfo(h,0x20000007,IntPtr.Zero,ref n);if(n==0)return "";IntPtr p=Marshal.AllocHGlobal((int)(n+1)*2);try{GetRawInputDeviceInfo(h,0x20000007,p,ref n);return Marshal.PtrToStringUni(p)??"";}finally{Marshal.FreeHGlobal(p);}}
}
