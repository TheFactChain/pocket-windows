using System;
using System.Collections.Generic;
using SMBLibrary.SMB2;

namespace SMBLibrary.Server
{
    public partial class SMBServer
    {
        // Validate the complete compound packet before any command can change a host file.
        private static bool ValidateIncomingSignatures(byte[] bytes,List<SMB2Command> commands,ConnectionState connection)
        {
            SMB2ConnectionState state=connection as SMB2ConnectionState;
            int offset=0;ulong previousSession=0;
            foreach(SMB2Command command in commands)
            {
                if(command.Header.IsRelatedOperations&&offset==0)return false;
                // Authentication changes the session table; a single pre-dispatch check cannot authenticate later commands.
                if(commands.Count>1&&(command is NegotiateRequest||command is SessionSetupRequest))return false;
                if(command.Header.NextCommand>(uint)(bytes.Length-offset))return false;
                int length=command.Header.NextCommand==0?bytes.Length-offset:(int)command.Header.NextCommand;
                if(length<SMB2Header.Length||length>bytes.Length-offset)return false;
                ulong sessionID=command.Header.SessionID;
                if(command.Header.IsRelatedOperations)sessionID=previousSession;
                else previousSession=sessionID;
                SMB2Session session=state==null?null:state.GetSession(sessionID);
                if(session!=null)
                {
                    if(!command.Header.IsSigned||session.SigningKey==null)return false;
                    byte[] message=new byte[length];Array.Copy(bytes,offset,message,0,length);
                    byte[] received=new byte[SMB2Header.SignatureLength];Array.Copy(message,SMB2Header.SignatureOffset,received,0,received.Length);
                    Array.Clear(message,SMB2Header.SignatureOffset,SMB2Header.SignatureLength);
                    byte[] expected=SMB2Cryptography.CalculateSignature(session.SigningKey,ToSMB2Dialect(state.Dialect),message,0,length);
                    int difference=0;for(int index=0;index<received.Length;index++)difference|=received[index]^expected[index];
                    if(difference!=0)return false;
                }
                else if(command.Header.IsSigned||!(command is NegotiateRequest||command is SessionSetupRequest||(command is EchoRequest&&sessionID==0)))return false;
                offset+=length;
            }
            return offset==bytes.Length;
        }
    }
}
